"""Small ACP v1 stdio client, independent of Hermes's private implementation.

The operator selects the OpenCode CLI (or its Windows batch launcher). All host
work is returned as remote tool calls; this module never executes model tools.
"""
import hashlib
import json
import math
import os
import queue
import shutil
import subprocess
import tempfile
import threading
import time
from types import SimpleNamespace
import uuid

from .constants import DEFAULT_MODEL, FREE_MODELS, VERSION, WORKSPACE, opencode_command
from .launcher import ProcessTree

BRIDGE_MODE = "build"
MAX_FRAME = 4 * 1024 * 1024
OPEN_TAG, CLOSE_TAG = "<hermes_tool_call>", "</hermes_tool_call>"
CONTRACT = (
    "REMOTE TOOL PROTOCOL: This conversation belongs to Hermes, not OpenCode. "
    "All tools described below are REMOTE Hermes tools. To request one, emit ONLY "
    '<hermes_tool_call>{"id":"unique_call_id","type":"function","function":'
    '{"name":"tool_name","arguments":"{...}"}}</hermes_tool_call> as your final TEXT response. '
    "arguments must be a JSON object encoded as a JSON string. Stop after requesting the tool; "
    "Hermes executes it AFTER your response and supplies the result in the next transcript. "
    "Do NOT use native OpenCode tools, shell, files, skills, or subagents: those are blocked. "
    "A blocked native tool does not mean the corresponding remote Hermes tool is unavailable. "
    "Do not claim an action happened before a real tool result exists. If the transcript "
    "already contains the needed tool result, answer from it. If no tool is needed, answer normally."
)
# Only infrastructure/non-secret behavior values are forwarded. In particular,
# no HERMES_*, vendor API keys, tokens, inline OpenCode configuration or YOLO
# settings from the parent are inherited. The CLI owns its normal login in HOME.
CHILD_ENV_NAMES = frozenset({
    "PATH", "HOME", "USER", "LOGNAME", "USERPROFILE", "APPDATA", "LOCALAPPDATA",
    "SYSTEMROOT", "SystemRoot", "WINDIR", "COMSPEC", "PATHEXT", "HOMEDRIVE", "HOMEPATH",
    "TMPDIR", "TEMP", "TMP",
    "XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_CACHE_HOME", "XDG_STATE_HOME", "XDG_RUNTIME_DIR",
    "LANG", "LC_ALL", "LC_CTYPE", "LC_MESSAGES", "TZ", "TERM",
    "SSL_CERT_FILE", "SSL_CERT_DIR", "NODE_EXTRA_CA_CERTS", "REQUESTS_CA_BUNDLE",
    "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY",
    "http_proxy", "https_proxy", "all_proxy", "no_proxy",
})


class Record(SimpleNamespace):
    """Structural OpenAI-compatible result with no SDK dependency."""
    def model_dump(self, *, exclude_none=False, **kwargs):
        def dump(value):
            if isinstance(value, SimpleNamespace):
                return {key: dump(item) for key, item in vars(value).items()
                        if not (exclude_none and item is None)}
            if isinstance(value, list):
                return [dump(item) for item in value]
            if isinstance(value, dict):
                return {key: dump(item) for key, item in value.items()}
            return value
        return dump(self)


class WorkerDied(RuntimeError):
    """The ACP process exited or its stdio closed before a response arrived."""


class RpcError(RuntimeError):
    """A JSON-RPC error response. Vendor text is never echoed or logged."""
    def __init__(self, method, code):
        super().__init__(f"OpenCode ACP {method} failed (code {code}); vendor error text was not logged")
        self.method = method
        self.code = code


class SessionState:
    def __init__(self, session_id, config, tools_signature):
        self.session_id = session_id
        self.config = config
        self.tools_signature = tools_signature
        self.represented = []
        self.last_used = time.monotonic()


class _Active:
    def __init__(self, rid, session_id, sinks):
        self.rid = rid
        self.session_id = session_id
        self.sinks = sinks


def child_environment():
    return {key: value for key, value in os.environ.items() if key in CHILD_ENV_NAMES}


def bridge_environment(config_home):
    environment = child_environment()
    environment["XDG_CONFIG_HOME"] = str(config_home)
    os.makedirs(config_home, exist_ok=True)
    return environment


def effective_timeout(value):
    if isinstance(value, (int, float)):
        seconds = float(value)
    else:
        values = [getattr(value, key, None) for key in ("read", "write", "connect", "pool")]
        seconds = max((float(item) for item in values if isinstance(item, (int, float))), default=900.0)
    if not math.isfinite(seconds) or seconds <= 0:
        raise ValueError("ACP timeout must be a finite positive number of seconds")
    return seconds


def options(option):
    """ACP select options can be flat or grouped. Never normalize model IDs."""
    result = []
    for item in option.get("options") or []:
        if not isinstance(item, dict):
            continue
        if isinstance(item.get("value"), str):
            result.append(item["value"])
        elif isinstance(item.get("options"), list):
            result.extend(child["value"] for child in item["options"]
                          if isinstance(child, dict) and isinstance(child.get("value"), str))
    return result


def _config_candidates(session, category):
    return [item for item in (session or {}).get("configOptions") or []
            if isinstance(item, dict) and isinstance(item.get("id"), str)
            and (item.get("id") == category or item.get("category") == category)]


def config_option(session, category, required=None):
    candidates = _config_candidates(session, category)
    for item in candidates:
        if required is None or required in options(item):
            return item
    offered = sorted({value for item in candidates for value in options(item)})
    raise RuntimeError(f"OpenCode ACP required {category} {required!r} was not advertised; offered={offered!r}. No prompt was sent.")


def optional_config_option(session, category, required=None):
    for item in _config_candidates(session, category):
        if required is None or required in options(item):
            return item
    return None


def selected_value(result, config_id):
    return next((item.get("currentValue") for item in (result or {}).get("configOptions", [])
                 if isinstance(item, dict) and item.get("id") == config_id), None)


def _plain(value):
    if isinstance(value, SimpleNamespace):
        return {key: _plain(item) for key, item in vars(value).items()}
    if isinstance(value, dict):
        return {key: _plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    return value


def render_content(content):
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        if any(isinstance(item, dict) and item.get("type") not in (None, "text", "input_text", "output_text") for item in content):
            raise ValueError("OpenCode Free ACP is text-only; image/audio attachments are not forwarded")
        return "\n".join(item if isinstance(item, str) else str(item.get("text", ""))
                         for item in content if isinstance(item, (str, dict)))
    return json.dumps(content, ensure_ascii=False)


def message_signature(message):
    data = _plain(message)
    if not isinstance(data, dict):
        data = {}
    tool_calls = []
    for call in data.get("tool_calls") or []:
        if not isinstance(call, dict):
            continue
        function = call.get("function") if isinstance(call.get("function"), dict) else {}
        arguments = function.get("arguments")
        if isinstance(arguments, str):
            try:
                arguments = json.loads(arguments)
            except json.JSONDecodeError:
                pass
        tool_calls.append({"id": call.get("id"), "name": function.get("name"), "arguments": arguments})
    payload = {
        "role": data.get("role"),
        "content": render_content(data.get("content")),
        "tool_calls": tool_calls,
        "tool_call_id": data.get("tool_call_id"),
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str).encode("utf-8")).hexdigest()


def prompt_signature(tools=None, tool_choice=None):
    payload = {"tools": tools or [], "tool_choice": tool_choice}
    return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str).encode("utf-8")).hexdigest()


def render_transcript(messages):
    parts = []
    for message in messages or []:
        data = _plain(message)
        if not isinstance(data, dict):
            continue
        role = str(data.get("role", "context"))
        label = role.capitalize()
        if role == "tool":
            label += " result for " + str(data.get("tool_call_id", "unknown"))
        content = render_content(data.get("content"))
        if content:
            parts.append(label + ":\n" + content)
        if data.get("tool_calls"):
            parts.append("Assistant's prior remote tool requests:\n" + json.dumps(data["tool_calls"], ensure_ascii=False, default=str))
    return "\n\n".join(parts)


def render_prompt(messages, tools=None, tool_choice=None):
    parts = [CONTRACT]
    if tools:
        parts.append("Remote Hermes tools (OpenAI function schemas):\n" + json.dumps(tools, ensure_ascii=False))
    if tool_choice is not None:
        parts.append("Remote tool choice: " + json.dumps(tool_choice, ensure_ascii=False))
    parts.append("Conversation transcript:")
    transcript = render_transcript(messages)
    if transcript:
        parts.append(transcript)
    parts.extend(["Continue from the latest user request, using actual tool results already in the transcript.", CONTRACT])
    return "\n\n".join(parts)


def render_delta(messages, start):
    transcript = render_transcript((messages or [])[start:])
    parts = ["Continuation of the same conversation. New transcript entries follow."]
    if transcript:
        parts.append(transcript)
    parts.append("Continue the same conversation from the new transcript entries. Use actual tool results already in the transcript.")
    return "\n\n".join(parts)


def parse_tool_calls(text, tools=None, tool_choice=None):
    allowed = {item["function"]["name"] for item in tools or []
               if isinstance(item, dict) and isinstance(item.get("function"), dict)
               and isinstance(item["function"].get("name"), str)}
    calls, prose, cursor, seen = [], [], 0, set()
    while True:
        start = text.find(OPEN_TAG, cursor)
        if start < 0:
            prose.append(text[cursor:])
            break
        prose.append(text[cursor:start])
        body_start = start + len(OPEN_TAG)
        body_start += len(text[body_start:]) - len(text[body_start:].lstrip())
        body, consumed = json.JSONDecoder().raw_decode(text[body_start:])
        end = body_start + consumed
        end += len(text[end:]) - len(text[end:].lstrip())
        if not text.startswith(CLOSE_TAG, end):
            raise ValueError("Incomplete remote tool-call envelope; no tool was dispatched")
        function = body.get("function") if isinstance(body, dict) else None
        if not isinstance(body, dict) or body.get("type") != "function" or not isinstance(function, dict) or function.get("name") not in allowed:
            raise ValueError("Unadvertised or invalid remote tool call; no tool was dispatched")
        if tool_choice == "none":
            raise ValueError("Remote tool call violates tool_choice=none")
        if isinstance(tool_choice, dict) and function["name"] != (tool_choice.get("function") or {}).get("name"):
            raise ValueError("Remote tool call violates the selected function")
        arguments = function.get("arguments", "{}")
        decoded = json.loads(arguments) if isinstance(arguments, str) else arguments
        if not isinstance(decoded, dict):
            raise ValueError("Remote tool arguments must be a JSON object")
        call_id = body.get("id", "acp_" + uuid.uuid4().hex)
        if not isinstance(call_id, str) or not call_id.strip() or call_id in seen:
            raise ValueError("Remote tool IDs must be nonempty and unique")
        seen.add(call_id)
        calls.append(Record(id=call_id, type="function", call_id=call_id, response_item_id=None,
                            function=Record(name=function["name"], arguments=json.dumps(decoded, ensure_ascii=False))))
        cursor = end + len(CLOSE_TAG)
    return calls, "\n".join(part.strip() for part in prose if part.strip())


class BridgeWorker:
    """One long-lived ACP process with an inbox, session history and request routing."""

    def __init__(self, command, args, *, cwd, env, initialize_timeout=30.0, on_spawn=None):
        self._send_lock = threading.Lock()
        self._state_lock = threading.Lock()
        self._close_lock = threading.Lock()
        self._inbox = queue.Queue()
        self._active = None
        self._dead = threading.Event()
        self._closed = False
        self._sessions = []
        self._sequence = 0
        self._cwd = cwd
        self.rpc_lock = threading.RLock()
        try:
            self.owner = ProcessTree(command, args, cwd=cwd, env=env)
        except OSError as exc:
            raise RuntimeError(f"Could not launch/contain OpenCode ACP executable {command!r}; "
                               "check its installation or set HERMES_OPENCODE_ACP_COMMAND to its absolute path") from exc
        self.proc = self.owner.proc
        if on_spawn is not None:
            on_spawn(self.owner)
        self._readers = [threading.Thread(target=self._pump, daemon=True),
                         threading.Thread(target=self._discard_stderr, daemon=True)]
        for reader in self._readers:
            reader.start()
        try:
            self.request("initialize", {
                "protocolVersion": 1,
                "clientCapabilities": {"fs": {"readTextFile": False, "writeTextFile": False}, "terminal": False},
                "clientInfo": {"name": "hermes-opencode-free-acp", "version": VERSION},
            }, timeout=initialize_timeout)
        except BaseException:
            self.close()
            raise

    @property
    def dead(self):
        return self._dead.is_set()

    def _send(self, message):
        wire = json.dumps(message, ensure_ascii=False)
        if len(wire.encode("utf-8")) > MAX_FRAME:
            raise ValueError("OpenCode ACP request exceeded the frame limit")
        with self._send_lock:
            if self._dead.is_set() or self.proc.poll() is not None:
                self._dead.set()
                raise WorkerDied("OpenCode ACP is no longer running")
            try:
                self.proc.stdin.write(wire + "\n")
                self.proc.stdin.flush()
            except (BrokenPipeError, OSError, ValueError) as exc:
                self._dead.set()
                raise WorkerDied("OpenCode ACP stdin closed") from exc

    def request(self, method, params, *, sinks=None, timeout=30.0):
        timeout = float(timeout)
        deadline = time.monotonic() + timeout
        with self._state_lock:
            if self._dead.is_set():
                raise WorkerDied("OpenCode ACP is no longer running")
            self._sequence += 1
            rid = self._sequence
            self._active = _Active(rid, (params or {}).get("sessionId"), sinks)
        try:
            self._send({"jsonrpc": "2.0", "id": rid, "method": method, "params": params})
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError(f"OpenCode ACP timed out during {method}")
                try:
                    message = self._inbox.get(timeout=min(0.1, max(0.001, remaining)))
                except queue.Empty:
                    continue
                if isinstance(message, BaseException):
                    raise WorkerDied(str(message)) from message
                if not isinstance(message, dict):
                    continue
                if message.get("id") != rid:
                    continue
                if "error" in message:
                    code = (message.get("error") or {}).get("code", "unknown")
                    raise RpcError(method, code)
                return message.get("result") or {}
        finally:
            with self._state_lock:
                if self._active is not None and self._active.rid == rid:
                    self._active = None

    def _pump(self):
        try:
            while True:
                line = self.proc.stdout.readline(MAX_FRAME + 1)
                if not line:
                    raise WorkerDied("OpenCode ACP exited before replying")
                if len(line) > MAX_FRAME:
                    raise ValueError("OpenCode ACP response exceeded the frame limit")
                message = json.loads(line)
                if not isinstance(message, dict):
                    raise ValueError("OpenCode ACP emitted a non-object frame")
                method = message.get("method")
                if isinstance(method, str):
                    if "id" in message:
                        self._answer(method, message["id"])
                    elif method == "session/update":
                        self._route(message)
                    continue
                self._inbox.put(message)
        except BaseException as exc:
            self._dead.set()
            with self._state_lock:
                self._active = None
            self._inbox.put(exc)

    def _answer(self, method, request_id):
        response = {"jsonrpc": "2.0", "id": request_id}
        if method == "session/request_permission":
            response["result"] = {"outcome": {"outcome": "cancelled"}}
        else:
            response["error"] = {"code": -32601, "message": "Native file, terminal and other ACP client operations are unavailable"}
        self._send(response)

    def _route(self, message):
        params = message.get("params") or {}
        update = params.get("update") or {}
        session_id = params.get("sessionId")
        with self._state_lock:
            active = self._active
            if active is None or active.sinks is None:
                return
            if active.session_id is not None and session_id is not None and session_id != active.session_id:
                return
            text_parts, reasoning_parts = active.sinks
        kind = update.get("sessionUpdate")
        if kind in ("tool_call", "tool_call_update"):
            return
        sink = {"agent_message_chunk": text_parts, "agent_thought_chunk": reasoning_parts}.get(kind)
        content = update.get("content") or {}
        if sink is not None and isinstance(content, dict) and isinstance(content.get("text"), str):
            sink.append(content["text"])

    def _discard_stderr(self):
        try:
            while self.proc.stderr.read(4096):
                pass
        except (OSError, ValueError):
            pass

    def new_session(self, timeout):
        result = self.request("session/new", {"cwd": self._cwd, "mcpServers": []}, timeout=timeout)
        if not isinstance(result.get("sessionId"), str) or not result["sessionId"]:
            raise RuntimeError("OpenCode ACP did not return a session ID")
        return result

    def add_session(self, result, tools_signature):
        session = SessionState(result["sessionId"], result, tools_signature)
        self._sessions.append(session)
        while len(self._sessions) > 8:
            oldest = min(self._sessions, key=lambda item: item.last_used)
            self._sessions.remove(oldest)
        return session

    def match_session(self, signatures, tools_signature):
        best = None
        for session in self._sessions:
            if session.tools_signature != tools_signature:
                continue
            if len(session.represented) > len(signatures):
                continue
            if all(left == right for left, right in zip(session.represented, signatures)):
                if best is None or len(session.represented) > len(best.represented):
                    best = session
        if best is None or len(best.represented) >= len(signatures):
            return None, None
        return best, len(best.represented)

    def forget(self, session):
        try:
            self._sessions.remove(session)
        except ValueError:
            pass

    def remember(self, session, signatures, assistant):
        session.represented = list(signatures) + [message_signature(assistant)]
        session.last_used = time.monotonic()

    def close(self):
        with self._close_lock:
            if self._closed:
                return
            self._closed = True
            self._dead.set()
            try:
                self.owner.stop()
            except Exception:
                pass
            self.owner.close()
            try:
                self.proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                try:
                    self.owner.stop(force=True)
                    self.proc.wait(timeout=2)
                except (subprocess.TimeoutExpired, OSError):
                    pass
            for stream in (self.proc.stdin, self.proc.stdout, self.proc.stderr):
                try:
                    stream.close()
                except (OSError, ValueError):
                    pass
            for reader in self._readers:
                reader.join(timeout=1)
            with self._state_lock:
                self._active = None
            self._sessions.clear()


class OpenCodeFreeClient:
    HERMES_SKIP_TRANSPORT_WRAP = True
    HERMES_SKIP_ASYNC_WRAP = True

    def __init__(self, *, command=None, args=None, acp_command=None, acp_args=None, **kwargs):
        self.command = acp_command or command or opencode_command()
        self.args = list(acp_args or args or ["acp", "--pure"])
        self.workspace = str(WORKSPACE)
        self.base_url = "acp://opencode-free"
        self.chat = Record(completions=Record(create=self._create_chat_completion))
        self.is_closed = False
        self._cancelled = threading.Event()
        self._lock = threading.Lock()
        self._processes = set()
        self._worker = None
        self._worker_lock = threading.Lock()
        self._config_home = None

    def cancel(self):
        with self._lock:
            self._cancelled.set()
            self.is_closed = True
            processes = tuple(self._processes)
            self._processes.clear()
            worker = self._worker
            self._worker = None
        for owner in processes:
            try:
                owner.stop()
            except Exception:
                pass
        if worker is not None:
            worker.close()

    def close(self):
        self.cancel()
        config_home = self._config_home
        self._config_home = None
        if config_home:
            shutil.rmtree(config_home, ignore_errors=True)

    def _register_owner(self, owner):
        with self._lock:
            self._processes.add(owner)

    def _ensure_worker(self, timeout):
        timeout = effective_timeout(timeout)
        with self._worker_lock:
            with self._lock:
                if self._cancelled.is_set():
                    raise RuntimeError("OpenCode ACP request was cancelled")
                worker = self._worker
            if worker is not None and not worker.dead:
                return worker
            if worker is not None:
                worker.close()
                with self._lock:
                    self._processes.discard(worker.owner)
                    if self._worker is worker:
                        self._worker = None
            with self._lock:
                if self._cancelled.is_set():
                    raise RuntimeError("OpenCode ACP request was cancelled")
                config_home = self._config_home
                if config_home is None:
                    config_home = tempfile.mkdtemp(prefix="opencode-free-acp-")
                    self._config_home = config_home
            spawned = {}

            def register(owner):
                spawned["owner"] = owner
                self._register_owner(owner)

            try:
                worker = BridgeWorker(self.command, self.args, cwd=self.workspace,
                                      env=bridge_environment(config_home),
                                      initialize_timeout=min(timeout, 30.0), on_spawn=register)
            except BaseException:
                with self._lock:
                    self._processes.discard(spawned.get("owner"))
                raise
            with self._lock:
                cancelled = self._cancelled.is_set()
                if not cancelled:
                    self._worker = worker
                    self.is_closed = False
            if cancelled:
                worker.close()
                with self._lock:
                    self._processes.discard(worker.owner)
                raise RuntimeError("OpenCode ACP request was cancelled")
            return worker

    def _discard_worker(self, worker=None):
        with self._lock:
            if worker is None:
                worker = self._worker
            if self._worker is worker:
                self._worker = None
        if worker is None:
            return
        worker.close()
        with self._lock:
            self._processes.discard(worker.owner)

    def list_models(self, *, timeout_seconds=15.0):
        timeout = effective_timeout(timeout_seconds)
        for attempt in (0, 1):
            worker = None
            try:
                worker = self._ensure_worker(timeout)
                with worker.rpc_lock:
                    session = worker.new_session(timeout)
                    offered = set(options(config_option(session, "model")))
                    return [model for model in FREE_MODELS if model in offered]
            except WorkerDied:
                if worker is not None:
                    self._discard_worker(worker)
                if attempt == 1:
                    raise

    def _select_config(self, worker, session, option, value, timeout, *, required):
        if option.get("currentValue") == value:
            return
        result = worker.request("session/set_config_option",
                                {"sessionId": session.session_id, "configId": option["id"], "value": value},
                                timeout=timeout)
        session.config = result
        if required and selected_value(result, option["id"]) != value:
            raise RuntimeError(f"OpenCode did not confirm requested {option['id']}={value!r}; no prompt was sent")

    def _prompt_session(self, worker, session, messages, start, *, model, tools, tool_choice, timeout):
        prompt = render_prompt(messages, tools, tool_choice) if start == 0 else render_delta(messages, start)
        mode = config_option(session.config, "mode", BRIDGE_MODE)
        chosen = config_option(session.config, "model", model)
        effort = optional_config_option(session.config, "effort", "low")
        self._select_config(worker, session, mode, BRIDGE_MODE, timeout, required=True)
        self._select_config(worker, session, chosen, model, timeout, required=True)
        if effort is not None:
            try:
                self._select_config(worker, session, effort, "low", timeout, required=False)
            except RpcError:
                pass
        text, reasoning = [], []
        worker.request("session/prompt", {"sessionId": session.session_id, "prompt": [{"type": "text", "text": prompt}]},
                       sinks=(text, reasoning), timeout=timeout)
        answer = "".join(text)
        if not answer.strip():
            raise RuntimeError("OpenCode returned no assistant text; not treating this as success")
        return answer, "".join(reasoning)

    def _prompt_on_worker(self, worker, messages, signatures, tools_signature, *, model, tools, tool_choice, timeout):
        with worker.rpc_lock:
            session, start = worker.match_session(signatures, tools_signature)
            if session is not None:
                try:
                    answer, reasoning = self._prompt_session(worker, session, messages, start,
                                                             model=model, tools=tools, tool_choice=tool_choice, timeout=timeout)
                    return answer, reasoning, worker, session
                except RpcError:
                    worker.forget(session)
                except BaseException:
                    worker.forget(session)
                    raise
            session = worker.add_session(worker.new_session(timeout), tools_signature)
            try:
                answer, reasoning = self._prompt_session(worker, session, messages, 0,
                                                         model=model, tools=tools, tool_choice=tool_choice, timeout=timeout)
                return answer, reasoning, worker, session
            except BaseException:
                worker.forget(session)
                raise

    def _run_prompt(self, messages, *, model, tools, tool_choice, timeout_seconds):
        if model not in FREE_MODELS:
            raise ValueError(f"OpenCode Free refuses an unapproved or paid model: {model!r}")
        timeout = effective_timeout(timeout_seconds)
        messages = messages or []
        signatures = [message_signature(message) for message in messages]
        tools_signature = prompt_signature(tools, tool_choice)
        for attempt in (0, 1):
            worker = None
            try:
                worker = self._ensure_worker(timeout)
                answer, reasoning, worker, session = self._prompt_on_worker(
                    worker, messages, signatures, tools_signature,
                    model=model, tools=tools, tool_choice=tool_choice, timeout=timeout)
                try:
                    calls, content = parse_tool_calls(answer, tools, tool_choice)
                except BaseException:
                    worker.forget(session)
                    raise
                worker.remember(session, signatures, Record(role="assistant", content=content, tool_calls=calls))
                return calls, content, reasoning
            except WorkerDied:
                if worker is not None:
                    self._discard_worker(worker)
                if self._cancelled.is_set():
                    raise RuntimeError("OpenCode ACP request was cancelled")
                if attempt == 1:
                    raise
            except TimeoutError:
                if worker is not None:
                    self._discard_worker(worker)
                raise

    def _create_chat_completion(self, *, model=None, messages=None, tools=None, tool_choice=None,
                                timeout=None, stream=False, **kwargs):
        model = model or DEFAULT_MODEL
        calls, content, reasoning = self._run_prompt(messages, model=model, tools=tools,
                                                     tool_choice=tool_choice, timeout_seconds=timeout)
        message = Record(role="assistant", content=content, tool_calls=calls,
                         reasoning=reasoning or None, reasoning_content=reasoning or None, reasoning_details=None)
        result = Record(id="acp_" + uuid.uuid4().hex, object="chat.completion", created=int(time.time()), model=model,
                        choices=[Record(index=0, message=message, finish_reason="tool_calls" if calls else "stop")], usage=None)
        if not stream:
            return result
        deltas = [Record(index=index, id=call.id, type="function", function=call.function) for index, call in enumerate(calls)]
        delta = Record(role="assistant", content=content or None, tool_calls=deltas or None,
                       reasoning_content=reasoning or None, reasoning=reasoning or None)
        return [Record(id=result.id, model=model, created=result.created, choices=[Record(index=0, delta=delta,
                       finish_reason=result.choices[0].finish_reason)], usage=None)]

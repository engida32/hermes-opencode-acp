"""Synthetic ACP server for offline tests. Never calls a model or reads user data."""
import io
import json
import os
import subprocess
import sys
import time

for stream in (sys.stdin, sys.stdout):
    if isinstance(stream, io.TextIOWrapper):
        stream.reconfigure(encoding="utf-8")

SCENARIO = sys.argv[1] if len(sys.argv) > 1 else "normal"
EXTRA = sys.argv[2] if len(sys.argv) > 2 else None
MODEL = "opencode/glm-5.3-flash-free"
SESSIONS = {}
COUNTER = 0
child = None
if SCENARIO == "descendant":
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"],
                             stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL)


def send(message):
    print(json.dumps(message), flush=True)


def config(session_id):
    values = SESSIONS[session_id]
    result = [
        {"id": "mode", "category": "mode", "currentValue": values["mode"],
         "options": [{"value": "plan"}] + ([] if SCENARIO == "missing-build" else [{"value": "build"}])},
        {"id": "model", "category": "model", "currentValue": values["model"],
         "options": [{"value": MODEL}, {"value": "openai/paid-test-only"}]},
    ]
    if SCENARIO != "missing-effort":
        result.append({"id": "effort", "category": "thought_level",
                       "currentValue": values.get("effort", "high"),
                       "options": [{"value": "low"}, {"value": "high"}, {"value": "default"}]})
    return result


def record(session_id, prompt):
    if EXTRA and SCENARIO in ("record", "missing-effort"):
        with open(EXTRA, "a", encoding="utf-8") as handle:
            handle.write(json.dumps({"sessionId": session_id, "prompt": prompt,
                                     "values": dict(SESSIONS[session_id])}) + "\n")


for line in sys.stdin:
    request = json.loads(line)
    method, rid = request.get("method"), request.get("id")
    if method == "initialize":
        if SCENARIO == "invalid-json":
            print("not-json", flush=True)
            continue
        result = {"protocolVersion": 1, "agentCapabilities": {}}
    elif method == "session/new":
        COUNTER += 1
        session_id = "fake-session-" + str(COUNTER)
        values = {"mode": "plan", "model": "openai/paid-test-only"}
        if SCENARIO != "missing-effort":
            values["effort"] = "high"
        SESSIONS[session_id] = values
        result = {"sessionId": session_id, "configOptions": config(session_id)}
    elif method == "session/set_config_option":
        params = request["params"]
        session_id = params["sessionId"]
        values = SESSIONS[session_id]
        if SCENARIO != "unconfirmed-model" or params["configId"] != "model":
            values[params["configId"]] = params["value"]
        result = {"configOptions": config(session_id)}
    elif method == "session/prompt":
        params = request["params"]
        session_id = params["sessionId"]
        prompt = params["prompt"][0]["text"]
        if SCENARIO == "exit-once" and EXTRA and not os.path.exists(EXTRA):
            with open(EXTRA, "w", encoding="utf-8") as handle:
                handle.write("used\n")
            sys.exit(3)
        if SCENARIO == "slow":
            time.sleep(30)
        if SCENARIO == "exit":
            sys.exit(3)
        if SCENARIO == "rpc-error":
            send({"jsonrpc": "2.0", "id": rid, "error": {"code": -32000, "message": "TEST_SENSITIVE_TEXT_MUST_NOT_BE_LOGGED"}})
            continue
        if SCENARIO == "native-tool":
            send({"jsonrpc": "2.0", "method": "session/update", "params": {"sessionId": session_id, "update": {"sessionUpdate": "tool_call", "title": "native read"}}})
            send({"jsonrpc": "2.0", "id": "server-native", "method": "session/request_permission", "params": {}})
            reply = json.loads(sys.stdin.readline())
            assert reply["result"]["outcome"]["outcome"] == "cancelled"
        if SCENARIO == "denied-requests":
            for index, operation in enumerate(("session/request_permission", "fs/read_text_file", "fs/write_text_file", "terminal/create")):
                send({"jsonrpc": "2.0", "id": "server-" + str(index), "method": operation, "params": {}})
                reply = json.loads(sys.stdin.readline())
                if operation == "session/request_permission":
                    assert reply["result"]["outcome"]["outcome"] == "cancelled"
                else:
                    assert reply["error"]["code"] == -32601
        record(session_id, prompt)
        text = "FAKE_ACP_OK"
        if SCENARIO == "pid":
            text = "PID:" + str(os.getpid())
        if SCENARIO in ("record", "missing-effort"):
            text = "OK"
        if SCENARIO == "exit-once":
            text = "OK"
        if SCENARIO == "descendant" and child is not None:
            text = "FAKE_CHILD_PID:" + str(child.pid)
        if SCENARIO == "unicode":
            text = "ACP café — 雪 👋"
            assert text in prompt
        if SCENARIO == "empty":
            text = ""
        if SCENARIO == "tool":
            text = '<hermes_tool_call>{"id":"fake-call","type":"function","function":{"name":"check_connection","arguments":"{\\"status\\":\\"ok\\"}"}}</hermes_tool_call>'
        if text:
            send({"jsonrpc": "2.0", "method": "session/update", "params": {"sessionId": session_id, "update": {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": text}}}})
        result = {"stopReason": "end_turn"}
    else:
        raise AssertionError("Unexpected synthetic request: " + str(method))
    send({"jsonrpc": "2.0", "id": rid, "result": result})

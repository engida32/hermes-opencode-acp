"""Offline unit and real-stdio transport tests; no live inference or credentials."""
import ast
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
import unittest
from unittest.mock import patch

from support import ROOT, client as acp, constants

TOOLS = [{"type": "function", "function": {"name": "check_connection", "parameters": {"type": "object"}}}]


def envelope(name="check_connection", arguments=None, call_id="test-call"):
    body = {"id": call_id, "type": "function", "function": {"name": name, "arguments": json.dumps(arguments or {"status": "ok"})}}
    return acp.OPEN_TAG + json.dumps(body) + acp.CLOSE_TAG


def fake(scenario="normal"):
    return acp.OpenCodeACPClient(command=sys.executable, args=[str(ROOT / "tests/fake_acp.py"), scenario])


class ProtocolTests(unittest.TestCase):
    def test_tool_round_trip(self):
        calls, text = acp.parse_tool_calls(envelope(), TOOLS)
        self.assertEqual(text, "")
        self.assertEqual(calls[0].function.name, "check_connection")
        self.assertEqual(json.loads(calls[0].function.arguments), {"status": "ok"})

    def test_nested_json_and_tag_like_strings(self):
        arguments = {"nested": {"list": [1, {"text": "braces } { and quotes \""}]}}
        calls, _ = acp.parse_tool_calls(envelope(arguments=arguments), TOOLS)
        self.assertEqual(json.loads(calls[0].function.arguments), arguments)

    def test_windows_tool_paths_are_preserved_literally(self):
        arguments = {"path": r"C:\Users\Test User\café\letter.txt", "content": "雪\nnext line"}
        calls, _ = acp.parse_tool_calls(envelope(arguments=arguments), TOOLS)
        self.assertEqual(json.loads(calls[0].function.arguments), arguments)

    def test_non_tool_prose_is_untouched(self):
        self.assertEqual(acp.parse_tool_calls("ordinary answer", TOOLS), ([], "ordinary answer"))

    def test_closing_tag_inside_arguments_is_not_the_envelope_end(self):
        arguments = {"content": "literal </hermes_tool_call> inside a source file"}
        calls, _ = acp.parse_tool_calls(envelope(arguments=arguments), TOOLS)
        self.assertEqual(json.loads(calls[0].function.arguments), arguments)

    def test_bare_json_does_not_execute_tools(self):
        self.assertEqual(acp.parse_tool_calls('{"name":"check_connection"}', TOOLS)[0], [])

    def test_native_tool_tags_do_not_execute_remote_tools(self):
        self.assertEqual(acp.parse_tool_calls("<tool_call>{}</tool_call>", TOOLS)[0], [])

    def test_unadvertised_tool_is_rejected(self):
        with self.assertRaises(ValueError):
            acp.parse_tool_calls(envelope(name="shell"), TOOLS)

    def test_no_tools_means_no_tool_execution(self):
        with self.assertRaises(ValueError):
            acp.parse_tool_calls(envelope(), [])

    def test_incomplete_envelope_is_rejected(self):
        with self.assertRaises(ValueError):
            acp.parse_tool_calls(acp.OPEN_TAG + "{}", TOOLS)

    def test_invalid_json_is_rejected(self):
        with self.assertRaises(ValueError):
            acp.parse_tool_calls(acp.OPEN_TAG + "broken" + acp.CLOSE_TAG, TOOLS)

    def test_non_object_envelope_is_rejected(self):
        with self.assertRaises(ValueError):
            acp.parse_tool_calls(acp.OPEN_TAG + "[]" + acp.CLOSE_TAG, TOOLS)

    def test_duplicate_tool_ids_are_rejected(self):
        with self.assertRaises(ValueError):
            acp.parse_tool_calls(envelope() + envelope(), TOOLS)

    def test_tool_choice_none_is_respected(self):
        with self.assertRaises(ValueError):
            acp.parse_tool_calls(envelope(), TOOLS, "none")

    def test_named_tool_choice_is_respected(self):
        with self.assertRaises(ValueError):
            acp.parse_tool_calls(envelope(), TOOLS, {"type": "function", "function": {"name": "different"}})

    def test_tool_record_serializes(self):
        call = acp.parse_tool_calls(envelope(), TOOLS)[0][0]
        self.assertEqual(json.loads(json.dumps(call.model_dump()))["function"]["name"], "check_connection")

    def test_history_keeps_tool_call_and_result_ids(self):
        prompt = acp.render_prompt([
            {"role": "assistant", "content": None, "tool_calls": [{"id": "history-id", "function": {"name": "read_file", "arguments": "{}"}}]},
            {"role": "tool", "tool_call_id": "history-id", "content": "real result"},
        ])
        self.assertIn("history-id", prompt)
        self.assertIn("real result", prompt)
        self.assertIn("prior remote tool requests", prompt)

    def test_image_attachments_fail_instead_of_disappearing(self):
        with self.assertRaises(ValueError):
            acp.render_content([{"type": "image_url", "image_url": {"url": "data:image/png;base64,TEST"}}])

    def test_plain_content_blocks_work(self):
        self.assertEqual(acp.render_content([{"type": "text", "text": "hello"}]), "hello")

    def test_grouped_options(self):
        self.assertEqual(acp.options({"options": [{"group": "free", "options": [{"value": "exact/model-id"}]}]}), ["exact/model-id"])

    def test_mode_identifier_does_not_require_category(self):
        option = {"id": "mode", "options": [{"value": "build"}]}
        self.assertEqual(acp.config_option({"configOptions": [option]}, "mode", "build"), option)

    def test_mode_category_accepts_other_identifier(self):
        option = {"id": "agent", "category": "mode", "options": [{"value": "build"}]}
        self.assertEqual(acp.config_option({"configOptions": [option]}, "mode", "build"), option)

    def test_missing_build_reports_offered_modes(self):
        with self.assertRaisesRegex(RuntimeError, "offered=.*plan"):
            acp.config_option({"configOptions": [{"id": "mode", "options": [{"value": "plan"}]}]}, "mode", "build")

    def test_nonfinite_or_zero_timeouts_are_rejected(self):
        for value in (0, -1, float("inf"), float("nan")):
            with self.subTest(value=value), self.assertRaises(ValueError):
                acp.effective_timeout(value)

    def test_timeout_object_is_supported(self):
        self.assertEqual(acp.effective_timeout(acp.Record(read=5, write=2)), 5)

    def test_child_environment_excludes_credentials_and_yolo(self):
        fixture = {"HOME": "/test-home", "PATH": "/test-path", "OPENAI_API_KEY": "NOT_A_REAL_TOKEN", "HERMES_YOLO_MODE": "1", "OPENCODE_CONFIG_CONTENT": "private test config"}
        with patch.dict(os.environ, fixture, clear=True):
            self.assertEqual(acp.child_environment(), {"HOME": "/test-home", "PATH": "/test-path"})

    def test_workspace_is_fixed_not_caller_selected(self):
        item = acp.OpenCodeACPClient(acp_cwd="/unrelated")
        self.assertEqual(item.workspace, str(constants.WORKSPACE))
        item.close()

    def test_paid_or_unknown_free_model_fails_before_spawn(self):
        for model in ("openai/paid-test-only", "opencode/invented-free"):
            item = fake()
            with patch.object(item, "_ensure_worker") as ensure, self.assertRaises(ValueError):
                item.chat.completions.create(model=model, messages=[])
            ensure.assert_not_called()
            item.close()

    def test_transport_build_is_not_a_bootstrap_variable(self):
        with patch.object(constants, "BRIDGE_MODE", "old-custom-agent", create=True):
            self.assertEqual(acp.BRIDGE_MODE, "build")

    def test_workspace_denies_native_tools_except_read(self):
        config = json.loads((ROOT / "workspace/opencode.json").read_text())
        self.assertEqual(config["permission"], {
            "*": "deny",
            "read": "allow",
            "external_directory": "deny",
            "doom_loop": "deny",
        })
        self.assertIs(config["snapshot"], False)
        self.assertEqual(config["default_agent"], "build")
        self.assertNotIn("build", config.get("agent", {}))
        self.assertNotIn("policies", config)

    def test_no_private_hermes_imports(self):
        for path in ROOT.glob("*.py"):
            for node in ast.walk(ast.parse(path.read_text())):
                if isinstance(node, ast.ImportFrom):
                    self.assertFalse((node.module or "").startswith(("agent.", "hermes_cli.")), (path.name, node.module))


class StdioTests(unittest.TestCase):
    def use(self, scenario="normal"):
        item = fake(scenario)
        self.addCleanup(item.close)
        return item

    def complete(self, item, **kwargs):
        return item.chat.completions.create(model=constants.DEFAULT_MODEL,
                                           messages=[{"role": "user", "content": "synthetic offline test"}], timeout=5, **kwargs)

    def test_real_stdio_handshake_and_exact_model_selection(self):
        result = self.complete(self.use())
        self.assertEqual(result.choices[0].message.content, "FAKE_ACP_OK")
        self.assertEqual(result.model, constants.DEFAULT_MODEL)
        self.assertIsNone(result.usage)

    def test_model_discovery_filters_paid_models(self):
        self.assertEqual(self.use().list_models(timeout_seconds=5), [constants.DEFAULT_MODEL])

    def test_real_stdio_remote_tool_parsing(self):
        result = self.complete(self.use("tool"), tools=TOOLS)
        self.assertEqual(result.choices[0].finish_reason, "tool_calls")
        self.assertEqual(result.choices[0].message.tool_calls[0].function.name, "check_connection")

    def test_stream_shape_has_unknown_usage(self):
        result = self.complete(self.use(), stream=True)
        self.assertEqual(result[0].choices[0].delta.content, "FAKE_ACP_OK")
        self.assertIsNone(result[0].usage)

    def test_unconfirmed_model_never_sends_prompt(self):
        with self.assertRaisesRegex(RuntimeError, "did not confirm"):
            self.complete(self.use("unconfirmed-model"))

    def test_missing_build_fails_before_inference(self):
        with self.assertRaisesRegex(RuntimeError, "required mode"):
            self.complete(self.use("missing-build"))

    def test_native_requests_are_denied(self):
        self.assertEqual(self.complete(self.use("denied-requests")).choices[0].message.content, "FAKE_ACP_OK")

    def test_native_tool_notification_is_denied_and_request_recovers(self):
        self.assertEqual(self.complete(self.use("native-tool")).choices[0].message.content, "FAKE_ACP_OK")

    def test_empty_response_is_not_success(self):
        with self.assertRaisesRegex(RuntimeError, "no assistant text"):
            self.complete(self.use("empty"))

    def test_early_process_exit_is_not_success(self):
        with self.assertRaises(RuntimeError):
            self.complete(self.use("exit"))

    def test_invalid_json_is_not_success(self):
        with self.assertRaises(RuntimeError):
            self.complete(self.use("invalid-json"))

    def test_vendor_error_text_is_not_logged_or_echoed(self):
        with self.assertRaises(RuntimeError) as caught:
            self.complete(self.use("rpc-error"))
        self.assertNotIn("TEST_SENSITIVE_TEXT", str(caught.exception))

    def test_timeout_reaps_owned_process(self):
        item = self.use("slow")
        with self.assertRaises(TimeoutError):
            item.chat.completions.create(model=constants.DEFAULT_MODEL, messages=[], timeout=0.5)
        self.assertEqual(item._processes, set())

    def test_cancel_before_start_does_not_spawn(self):
        item = self.use()
        item.cancel()
        with patch.object(subprocess, "Popen") as spawn, self.assertRaisesRegex(RuntimeError, "cancelled"):
            self.complete(item)
        spawn.assert_not_called()

    def test_cancel_inflight_is_prompt_and_reaps_process(self):
        item, errors = self.use("slow"), []
        def run():
            try:
                self.complete(item)
            except Exception as exc:
                errors.append(exc)
        thread = threading.Thread(target=run)
        thread.start()
        deadline = time.monotonic() + 3
        while not item._processes and time.monotonic() < deadline:
            time.sleep(0.01)
        item.cancel()
        thread.join(timeout=5)
        self.assertFalse(thread.is_alive())
        self.assertTrue(errors)
        self.assertEqual(item._processes, set())

    def test_normal_request_keeps_the_process_warm(self):
        item = self.use()
        self.complete(item)
        self.assertEqual(len(item._processes), 1)
        self.assertFalse(item.is_closed)
        item.close()
        self.assertEqual(item._processes, set())
        self.assertTrue(item.is_closed)

    def test_cancel_racing_process_startup_reaps_the_late_child(self):
        item, errors = self.use(), []
        entered, release = threading.Event(), threading.Event()
        original = subprocess.Popen
        def delayed(*args, **kwargs):
            entered.set()
            release.wait(timeout=3)
            return original(*args, **kwargs)
        def run():
            try:
                self.complete(item)
            except Exception as exc:
                errors.append(exc)
        with patch.object(subprocess, "Popen", delayed):
            thread = threading.Thread(target=run)
            thread.start()
            self.assertTrue(entered.wait(timeout=3))
            item.cancel()
            release.set()
            thread.join(timeout=5)
        self.assertFalse(thread.is_alive())
        self.assertTrue(errors)
        self.assertEqual(item._processes, set())


if __name__ == "__main__":
    unittest.main()

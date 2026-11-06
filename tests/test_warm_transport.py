"""Warm-process and session-reuse tests; no live inference or credentials."""
import json
import os
import sys
import tempfile
import unittest
from unittest.mock import patch

from support import ROOT, client as acp, constants

TOOLS = [{"type": "function", "function": {"name": "check_connection", "parameters": {"type": "object"}}}]
OTHER_TOOLS = [{"type": "function", "function": {"name": "other_tool", "parameters": {"type": "object"}}}]
USER = "synthetic offline test"
MODEL = constants.DEFAULT_MODEL


def fake(scenario="normal", extra=None):
    args = [str(ROOT / "tests/fake_acp.py"), scenario]
    if extra is not None:
        args.append(extra)
    return acp.OpenCodeACPClient(command=sys.executable, args=args)


def content(result):
    return result.choices[0].message.content


def complete(client, messages=None, tools=None, timeout=5):
    return client.chat.completions.create(
        model=MODEL,
        messages=messages or [{"role": "user", "content": USER}],
        tools=tools,
        timeout=timeout,
    )


class IsolationAndSignatureTests(unittest.TestCase):
    def test_bridge_environment_uses_private_config_home_and_drops_secrets(self):
        with tempfile.TemporaryDirectory() as tmp:
            config_home = os.path.join(tmp, "config")
            fixture = {
                "PATH": "/test-path",
                "HOME": "/test-home",
                "XDG_CONFIG_HOME": "/global/config",
                "OPENCODE_CONFIG_CONTENT": "private test config",
                "OPENAI_API_KEY": "NOT_A_REAL_TOKEN",
                "HERMES_YOLO_MODE": "1",
            }
            with patch.dict(os.environ, fixture, clear=True):
                environment = acp.bridge_environment(config_home)
            self.assertEqual(environment["XDG_CONFIG_HOME"], config_home)
            self.assertTrue(os.path.isdir(config_home))
            self.assertEqual(environment["HOME"], "/test-home")
            self.assertEqual(environment["PATH"], "/test-path")
            self.assertNotIn("OPENCODE_CONFIG_CONTENT", environment)
            self.assertNotIn("OPENAI_API_KEY", environment)
            self.assertNotIn("HERMES_YOLO_MODE", environment)

    def test_message_signature_ignores_presentation_fields(self):
        first = {
            "role": "assistant",
            "content": "hello",
            "tool_calls": [{"id": "c1", "type": "function", "function": {"name": "f", "arguments": "{}"}}],
            "reasoning": "not part of the transcript",
        }
        second = acp.Record(
            role="assistant",
            content="hello",
            tool_calls=[acp.Record(id="c1", type="function", function=acp.Record(name="f", arguments="{}"))],
        )
        self.assertEqual(acp.message_signature(first), acp.message_signature(second))
        self.assertNotEqual(acp.message_signature(first), acp.message_signature({"role": "assistant", "content": "bye"}))

    def test_render_delta_contains_only_new_transcript_entries(self):
        messages = [
            {"role": "user", "content": "first request"},
            {"role": "assistant", "content": "first answer"},
            {"role": "tool", "tool_call_id": "call-1", "content": "tool result 123"},
        ]
        delta = acp.render_delta(messages, 2)
        self.assertIn("tool result 123", delta)
        self.assertNotIn("first request", delta)
        self.assertNotIn("first answer", delta)
        self.assertNotIn("REMOTE TOOL PROTOCOL:", delta)

    def test_optional_config_option_returns_none_when_absent_or_unoffered(self):
        session = {"configOptions": [{"id": "effort", "category": "thought_level",
                                      "currentValue": "high", "options": [{"value": "low"}, {"value": "high"}]}]}
        self.assertEqual(acp.optional_config_option(session, "effort", "low")["id"], "effort")
        self.assertIsNone(acp.optional_config_option(session, "effort", "max"))
        self.assertIsNone(acp.optional_config_option({"configOptions": []}, "effort", "low"))

    def test_default_command_line_is_pure(self):
        item = acp.OpenCodeACPClient()
        self.addCleanup(item.close)
        self.assertEqual(item.args, ["acp", "--pure"])


class WarmProcessTests(unittest.TestCase):
    def temp_path(self):
        handle, path = tempfile.mkstemp(prefix="acp-record-", suffix=".jsonl")
        os.close(handle)
        os.unlink(path)
        self.addCleanup(lambda: os.path.exists(path) and os.unlink(path))
        return path

    def use(self, scenario="normal", extra=None):
        item = fake(scenario, extra)
        self.addCleanup(item.close)
        return item

    @staticmethod
    def lines(path):
        with open(path, encoding="utf-8") as handle:
            return [json.loads(line) for line in handle if line.strip()]

    def test_one_process_serves_two_calls_and_stays_open(self):
        item = self.use("pid")
        first = content(complete(item))
        second = content(complete(item))
        self.assertRegex(first, r"^PID:\d+$")
        self.assertEqual(first, second)
        self.assertEqual(len(item._processes), 1)
        self.assertFalse(item.is_closed)

    def test_close_reaps_the_warm_process(self):
        item = self.use()
        self.assertEqual(content(complete(item)), "FAKE_ACP_OK")
        self.assertEqual(len(item._processes), 1)
        item.close()
        self.assertEqual(item._processes, set())
        self.assertTrue(item.is_closed)

    def test_contiguous_history_reuses_session_and_sends_only_delta(self):
        path = self.temp_path()
        item = self.use("record", path)
        first = complete(item)
        assistant = {"role": "assistant", "content": content(first)}
        second = complete(item, messages=[
            {"role": "user", "content": USER},
            assistant,
            {"role": "tool", "tool_call_id": "call-1", "content": "tool result 123"},
        ])
        lines = self.lines(path)
        self.assertEqual(len(lines), 2)
        self.assertEqual(lines[0]["sessionId"], lines[1]["sessionId"])
        self.assertIn(USER, lines[0]["prompt"])
        self.assertNotIn(USER, lines[1]["prompt"])
        self.assertIn("tool result 123", lines[1]["prompt"])
        self.assertNotIn("REMOTE TOOL PROTOCOL:", lines[1]["prompt"])
        self.assertEqual(content(second), "OK")

    def test_changed_history_uses_a_fresh_session(self):
        path = self.temp_path()
        item = self.use("record", path)
        first = complete(item)
        assistant = {"role": "assistant", "content": content(first)}
        complete(item, messages=[
            {"role": "user", "content": "different request"},
            assistant,
            {"role": "tool", "tool_call_id": "call-1", "content": "tool result 123"},
        ])
        lines = self.lines(path)
        self.assertNotEqual(lines[0]["sessionId"], lines[1]["sessionId"])
        self.assertIn("REMOTE TOOL PROTOCOL:", lines[1]["prompt"])

    def test_changed_tool_schema_uses_a_fresh_session(self):
        path = self.temp_path()
        item = self.use("record", path)
        first = complete(item)
        assistant = {"role": "assistant", "content": content(first)}
        complete(item, messages=[{"role": "user", "content": USER}, assistant], tools=TOOLS)
        lines = self.lines(path)
        self.assertNotEqual(lines[0]["sessionId"], lines[1]["sessionId"])
        self.assertIn("check_connection", lines[1]["prompt"])

    def test_effort_low_is_selected_when_advertised(self):
        path = self.temp_path()
        item = self.use("record", path)
        complete(item)
        self.assertEqual(self.lines(path)[0]["values"]["effort"], "low")

    def test_missing_effort_option_is_tolerated(self):
        path = self.temp_path()
        item = self.use("missing-effort", path)
        self.assertEqual(content(complete(item)), "OK")
        self.assertNotIn("effort", self.lines(path)[0]["values"])

    def test_worker_death_transparently_respawns_once(self):
        marker = self.temp_path()
        item = self.use("exit-once", marker)
        self.assertEqual(content(complete(item)), "OK")
        self.assertEqual(len(item._processes), 1)
        self.assertTrue(os.path.exists(marker))

    def test_paid_or_unknown_model_fails_before_spawn(self):
        for model in ("openai/paid-test-only", "opencode/invented-free"):
            item = self.use()
            with patch.object(item, "_ensure_worker") as ensure, self.assertRaises(ValueError):
                item.chat.completions.create(model=model, messages=[])
            ensure.assert_not_called()


if __name__ == "__main__":
    unittest.main()

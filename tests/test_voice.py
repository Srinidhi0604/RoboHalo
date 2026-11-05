import json
import os
import tempfile
import unittest
from pathlib import Path

from agent import voice
from agent.run_agent import choose_tool, load_env, step_cost
from tests.test_agent import tiny_jpeg


class VoiceTests(unittest.TestCase):
    def test_parse_recogniser_output(self):
        self.assertEqual(voice.parse_heard("0.83|bring the red cube"), ("bring the red cube", 0.83))
        self.assertEqual(voice.parse_heard("NONE|"), (None, 0.0))
        with self.assertRaises(RuntimeError):
            voice.parse_heard("ERROR|no microphone")

    def test_confirmed_command_is_used_as_heard(self):
        said = []
        command = voice.hear_command(lambda: ("bring the blue sphere", 0.9), confirm=lambda prompt: "", say=said.append)
        self.assertEqual(command, "bring the blue sphere")
        self.assertTrue(any("bring the blue sphere" in line for line in said))

    def test_retry_then_typed_correction(self):
        heard = iter([("bring the read cue", 0.5), ("bring the red queue", 0.6)])
        answers = iter(["r", "Bring the red cube"])
        command = voice.hear_command(lambda: next(heard), confirm=lambda prompt: next(answers), say=lambda line: None)
        self.assertEqual(command, "Bring the red cube")

    def test_hands_free_mode_rejects_unclear_speech(self):
        heard = iter([(None, 0.0), ("mumble", 0.2), ("fetch the tallest object", 0.8)])
        command = voice.hear_command(lambda: next(heard), auto_yes=True, say=lambda line: None)
        self.assertEqual(command, "fetch the tallest object")
        self.assertIsNone(voice.hear_command(lambda: (None, 0.0), auto_yes=True, say=lambda line: None))


class ApiWiringTests(unittest.TestCase):
    """The real Anthropic SDK builds and parses the request; only the network is replaced."""

    def test_request_built_by_the_sdk_and_response_parsed(self):
        import anthropic
        import httpx
        seen = {}

        def handler(request):
            seen["headers"] = dict(request.headers)
            seen["body"] = json.loads(request.content)
            return httpx.Response(200, json={
                "id": "msg_1", "type": "message", "role": "assistant", "model": "claude-sonnet-5-5",
                "content": [{"type": "text", "text": "Something is ahead on the left."},
                            {"type": "tool_use", "id": "toolu_1", "name": "turn", "input": {"degrees": -30}}],
                "stop_reason": "tool_use", "stop_sequence": None,
                "usage": {"input_tokens": 400, "output_tokens": 30, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 900}})

        client = anthropic.Anthropic(api_key="test-key", http_client=httpx.Client(transport=httpx.MockTransport(handler)))
        name, args, usage, reasoning, call_id = choose_tool(
            client, "claude-sonnet-5-5", "Fetch it", tiny_jpeg(), 1.2, {"holding": False, "retry_count": 0, "x": 1.0, "y": 2.0, "heading": 90}, [])
        self.assertEqual((name, args, call_id), ("turn", {"degrees": -30}, "toolu_1"))
        self.assertEqual(reasoning, "Something is ahead on the left.")
        body, headers = seen["body"], seen["headers"]
        self.assertEqual(body["model"], "claude-sonnet-5-5")
        self.assertEqual(body["output_config"], {"effort": "low"})
        self.assertEqual(body["fallbacks"], "default")
        self.assertIn("server-side-fallback-2026-07-01", headers["anthropic-beta"])
        self.assertEqual(body["tool_choice"], {"type": "auto", "disable_parallel_tool_use": True})
        self.assertEqual(body["system"][0]["cache_control"], {"type": "ephemeral"})
        self.assertEqual(body["messages"][0]["content"][0]["type"], "image")
        self.assertIn("Position: x=1.00, y=2.00, heading=90 deg", body["messages"][0]["content"][1]["text"])
        self.assertEqual(headers["x-api-key"], "test-key")
        self.assertGreater(step_cost("claude-sonnet-5-5", usage), 0)  # cache read tokens are priced

    def test_env_file_loads_key_without_overwriting(self):
        with tempfile.TemporaryDirectory() as d:
            env = Path(d) / ".env"
            env.write_text('ROBOHALO_TEST_KEY="abc123"\n# comment\nROBOHALO_KEEP=new\n')
            os.environ["ROBOHALO_KEEP"] = "old"
            try:
                load_env(env)
                self.assertEqual(os.environ["ROBOHALO_TEST_KEY"], "abc123")
                self.assertEqual(os.environ["ROBOHALO_KEEP"], "old")
            finally:
                os.environ.pop("ROBOHALO_TEST_KEY", None)
                os.environ.pop("ROBOHALO_KEEP", None)


if __name__ == "__main__":
    unittest.main()


class VoiceFallbackTests(unittest.TestCase):
    def test_no_microphone_falls_back_to_typing(self):
        def broken():
            raise RuntimeError("no audio from the microphone")
        said = []
        command = voice.hear_command(broken, confirm=lambda prompt: "Bring the red cube", say=said.append)
        self.assertEqual(command, "Bring the red cube")
        self.assertTrue(any("Voice input unavailable" in line for line in said))
        self.assertIsNone(voice.hear_command(broken, auto_yes=True, say=lambda line: None))


class SpeakDecisionTests(unittest.TestCase):
    """Speak: Claude only decides colour + shape; the engine's routine acts on that decision."""

    def run_speak(self, answer):
        from types import SimpleNamespace as NS
        from agent import speak
        calls, told = [], []
        client = NS(beta=NS(messages=NS(create=lambda **kw: calls.append(kw) or NS(content=[NS(type="tool_use", name="decide", input=answer)]))))
        tiny = __import__("io").BytesIO()
        __import__("PIL.Image").Image.new("RGB", (8, 8)).save(tiny, "JPEG")

        class Engine:
            posts = []
            def call(self, path, body=None, timeout=30):
                if body is not None: self.posts.append((path, body))
                return tiny.getvalue() if path == "/look" else {"state": "Done", "outcome": "pass"} if path == "/state" else {"ok": True}
        with tempfile.TemporaryDirectory() as folder:
            record = speak.handle("bring the read ball", client, "claude-opus-5-5", Engine(), told.append, folder)
            run = next(Path(folder).glob("speak-*"))
            self.assertTrue({"run.jsonl", "replay.html", "frame-001.png", "frame-009.png"} <= {f.name for f in run.iterdir()})
        return record, calls, told, Engine.posts

    def test_decision_is_sent_back_then_the_engine_acts(self):
        record, calls, told, posts = self.run_speak({"found": True, "colour": "red", "shape": "sphere", "view": 3, "reason": "A red ball in view 3."})
        self.assertEqual(len(calls), 1)                                            # one decision call, no driving by the model
        self.assertTrue(any("Claude decided: red sphere" in line for line in told))
        self.assertIn("Bring the red sphere", [b["command"] for p, b in posts if p == "/run_builtin"])
        self.assertEqual(record["outcome"], "pass")
        self.assertEqual(record["step_count"], 11)   # 8 looks, 1 decision, manual pickup, manual drop

    def test_no_match_means_no_action(self):
        record, _, told, posts = self.run_speak({"found": False, "reason": "No ball in any view."})
        self.assertFalse([p for p in posts if p[0] == "/run_builtin"])
        self.assertIn("nothing here fits", told[-1])

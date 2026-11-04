import io
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from PIL import Image

from agent.run_agent import SYSTEM, TOOLS, choose_tool, request_kwargs, run, step_cost, validate


def tiny_jpeg():
    buffer = io.BytesIO()
    Image.new("RGB", (64, 48), (90, 90, 90)).save(buffer, "JPEG")
    return buffer.getvalue()


class FakeEngine:
    def __init__(self, collide_first_move=False):
        self.done = False
        self.calls = []
        self.collide = collide_first_move

    def call(self, path, body=None):
        self.calls.append((path, body))
        if path == "/look": return tiny_jpeg()
        if path == "/depth": return 1.2
        if path == "/record": return {"recording": False, "frames": 0}
        if path == "/state": return {"state": "Done" if self.done else "Locate", "holding": False, "retry_count": 0, "outcome": "pass" if self.done else None}
        if path == "/drop": self.done = True
        if path == "/move" and self.collide:
            self.collide = False
            return {"ok": False, "collided": True, "moved": 0.3}
        return {"ok": True, "success": True, "in_zone": self.done}


def usage(**kw):
    base = dict(input_tokens=10, output_tokens=2, cache_creation_input_tokens=0, cache_read_input_tokens=0)
    base.update(kw)
    return SimpleNamespace(**base)


class FakeMessages:
    def __init__(self, calls):
        self.calls = list(calls)
        self.requests = []

    def create(self, **kwargs):
        self.requests.append(kwargs)
        name, args = self.calls.pop(0)
        content = [SimpleNamespace(type="text", text="The target is ahead.")]
        if name:
            content.append(SimpleNamespace(type="tool_use", id=f"toolu_{len(self.requests)}", name=name, input=args))
        return SimpleNamespace(content=content, stop_reason="tool_use", usage=usage())


def client(calls):
    messages = FakeMessages(calls)
    return SimpleNamespace(beta=SimpleNamespace(messages=messages)), messages


class AgentTests(unittest.TestCase):
    def test_payload_has_image_and_no_scene_metadata(self):
        c, messages = client([("turn", {"degrees": 30})])
        name, args, _, reasoning, _ = choose_tool(c, "claude-sonnet-5-5", "Bring the round red thing", b"jpeg", 2.0, {"holding": False, "retry_count": 0}, [])
        self.assertEqual((name, args), ("turn", {"degrees": 30}))
        self.assertEqual(reasoning, "The target is ahead.")
        payload = json.dumps(messages.requests[0])
        self.assertIn('"type": "image"', payload)
        for forbidden in ("object_ids", "object_positions", "drop_zone", "zone_x"):
            self.assertNotIn(forbidden, payload)

    def test_request_follows_sonnet_5_5_rules_and_caches_prefix(self):
        kwargs = request_kwargs("claude-sonnet-5-5", [{"role": "user", "content": "x"}])
        self.assertEqual(kwargs["tool_choice"]["type"], "auto")           # forced tool_choice is a 400 on Sonnet 5.5
        self.assertTrue(kwargs["tool_choice"]["disable_parallel_tool_use"])
        self.assertEqual(kwargs["system"][0]["cache_control"], {"type": "ephemeral"})
        self.assertEqual(kwargs["extra_body"]["fallbacks"], "default")
        self.assertNotIn("fallbacks", request_kwargs("claude-haiku-5-5", [])["extra_body"])  # no server fallback for Haiku
        self.assertNotIn("thinking", kwargs)  # adaptive (default) at low effort

    def test_static_prefix_is_long_enough_to_cache(self):
        # Sonnet/Haiku 5.5 need >= 512 tokens in the cached prefix; ~4 chars per token.
        self.assertGreater(len(SYSTEM + json.dumps(TOOLS)) / 4, 600)

    def test_validation_rejects_unsafe_motion(self):
        with self.assertRaises(ValueError): validate("move", {"distance": 2})

    def test_run_writes_png_frames_jsonl_and_cost(self):
        engine = FakeEngine()
        c, _ = client([("drop", {})])
        with tempfile.TemporaryDirectory() as directory:
            summary = run("Bring the round red thing", c, "claude-sonnet-5-5", engine, directory, 3)
            self.assertEqual(summary["outcome"], "pass")
            self.assertGreater(summary["cost_estimate_usd"], 0)
            log = next(Path(directory).glob("*/run.jsonl")).read_text().splitlines()
            self.assertEqual(len(log), 2)
            first = json.loads(log[0])
            for key in ("step", "timestamp", "tool", "args", "result", "frame_path"):
                self.assertIn(key, first)
            self.assertEqual(first["reasoning"], "The target is ahead.")
            self.assertTrue(next(Path(directory).glob("*/frame-001.png")).exists())

    def test_no_new_image_after_pickup(self):
        engine = FakeEngine()
        c, messages = client([("pickup", {}), ("drop", {})])
        with tempfile.TemporaryDirectory() as directory:
            run("Bring the round red thing", c, "claude-sonnet-5-5", engine, directory, 3)
        self.assertEqual(sum(path == "/look" for path, _ in engine.calls), 1)
        second = messages.requests[1]["messages"]
        self.assertNotIn('"type": "image"', json.dumps(second))  # no image sent at all on the step after pickup
        self.assertEqual(second[-1]["content"][0]["type"], "tool_result")  # pickup result returned as the tool result

    def test_bad_tool_input_is_reported_and_run_continues(self):
        engine = FakeEngine()
        c, messages = client([("move", {"distance": 5}), ("drop", {})])
        with tempfile.TemporaryDirectory() as directory:
            summary = run("Fetch it", c, "claude-sonnet-5-5", engine, directory, 5)
        self.assertEqual(summary["outcome"], "pass")
        self.assertIn("between -1 and 1", json.dumps(messages.requests[1]["messages"][-1]))

    def test_collision_result_reaches_claude_in_history(self):
        engine = FakeEngine(collide_first_move=True)
        c, messages = client([("move", {"distance": 1}), ("drop", {})])
        with tempfile.TemporaryDirectory() as directory:
            run("Fetch it", c, "claude-sonnet-5-5", engine, directory, 5)
        last = messages.requests[1]["messages"][-1]["content"][0]
        self.assertEqual(last["type"], "tool_result")
        self.assertIn('"collided":true', last["content"][-1]["text"])

    def test_new_frame_travels_inside_the_tool_result(self):
        engine = FakeEngine()
        c, messages = client([("turn", {"degrees": 45}), ("drop", {})])
        with tempfile.TemporaryDirectory() as directory:
            run("Fetch it", c, "claude-sonnet-5-5", engine, directory, 5)
        msgs = messages.requests[1]["messages"]
        self.assertEqual([m["role"] for m in msgs], ["user", "assistant", "user"])
        self.assertEqual(msgs[1]["content"][0]["type"], "tool_use")
        result = msgs[2]["content"][0]
        self.assertEqual(result["tool_use_id"], msgs[1]["content"][0]["id"])
        self.assertEqual(result["content"][0]["type"], "image")
        self.assertEqual(json.dumps(msgs).count('"type": "image"'), 1)  # only the latest frame

    def test_hard_stop_at_max_steps(self):
        engine = FakeEngine()
        c, _ = client([("turn", {"degrees": 45})] * 4)
        with tempfile.TemporaryDirectory() as directory:
            summary = run("Fetch it", c, "claude-sonnet-5-5", engine, directory, 4)
        self.assertEqual((summary["outcome"], summary["step_count"]), ("fail_not_reached", 4))

    def test_step_cost_uses_cache_prices(self):
        cost = step_cost("claude-sonnet-5-5", usage(input_tokens=1_000_000, output_tokens=0))
        cached = step_cost("claude-sonnet-5-5", usage(input_tokens=0, cache_read_input_tokens=1_000_000, output_tokens=0))
        self.assertAlmostEqual(cost, 2.0)
        self.assertAlmostEqual(cached, 0.2)


if __name__ == "__main__":
    unittest.main()

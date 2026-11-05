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


FORBIDDEN = ("red", "blue", "yellow", "green", "cube", "sphere", "cylinder", "cone", "sphere", "pallet", "warehouse",
             "object_id", "object_pos", "zone_x", "zone_y")


class BoundaryAuditTests(unittest.TestCase):
    """A3: nothing privileged reaches Claude, and every image Claude saw is on disk and in the log."""

    def run_session(self, directory):
        engine = FakeEngine(collide_first_move=True)
        c, messages = client([("turn", {"degrees": 45}), ("move", {"distance": 1}), ("pickup", {}), ("drop", {})])
        run("Fetch the thing I asked for", c, "claude-sonnet-5-5", engine, directory, 6)
        return engine, messages.requests

    def test_payloads_contain_no_scene_facts(self):
        with tempfile.TemporaryDirectory() as directory:
            _, requests = self.run_session(directory)
            for request in requests:
                text = json.dumps({k: v for k, v in request.items() if k != "messages"}).lower()  # system + tools
                for message in request["messages"]:
                    blocks = message["content"] if isinstance(message["content"], list) else [{"type": "text", "text": message["content"]}]
                    for block in blocks:
                        if block["type"] == "text":
                            text += " " + block["text"].lower()
                        elif block["type"] == "tool_result":
                            text += " " + json.dumps([b for b in block["content"] if b.get("type") == "text"]).lower()
                        elif block["type"] == "tool_use":
                            text += " " + json.dumps(block["input"]).lower()
                import re
                for word in FORBIDDEN:
                    self.assertIsNone(re.search(rf"{word}", text), word)

    def test_every_image_sent_has_a_saved_frame_and_a_log_entry(self):
        with tempfile.TemporaryDirectory() as directory:
            _, requests = self.run_session(directory)
            sent = sum(json.dumps(r["messages"]).count('"type": "image"') for r in requests)
            run_dir = next(Path(directory).glob("2*"))
            entries = [json.loads(line) for line in (run_dir / "run.jsonl").read_text().splitlines()]
            logged = [e["frame_path"] for e in entries if "step" in e and e["frame_path"]]
            self.assertEqual(sent, len(logged))
            for name in logged:
                self.assertTrue((run_dir / name).exists(), name)

    def test_engine_error_strings_do_not_describe_the_scene(self):
        source = Path(r"E:\turtlebot3-unity-rossharp-main\turtlebot3-unity-rossharp-main\Assets\Scripts\RobotEngine.cs")
        if not source.exists():
            self.skipTest("Unity project not on this machine")
        import re
        for message in re.findall(r'Error\(context, \d+, "([^"]+)"\)', source.read_text(encoding="utf-8")):
            for word in ("red", "blue", "yellow", "cube", "sphere", "cylinder", "cone", "position", "zone"):
                self.assertNotRegex(message.lower(), rf"\b{word}\b", message)

    def test_camera_contract_in_engine_source(self):
        source = Path(r"E:\turtlebot3-unity-rossharp-main\turtlebot3-unity-rossharp-main\Assets\Scripts\RobotEngine.cs")
        if not source.exists():
            self.skipTest("Unity project not on this machine")
        text = source.read_text(encoding="utf-8")
        self.assertIn("new Vector3(0, 0.5f, 0.28f)", text)      # 0.5 m above the floor, at the front
        self.assertIn("Quaternion.Euler(10, 0, 0)", text)       # 10 degrees down
        self.assertIn("fieldOfView = 73.74f", text)             # 90 degrees horizontal at 4:3
        self.assertIn("new RenderTexture(640, 480", text)       # 640x480


class GoToAndReplayTests(unittest.TestCase):
    def test_go_to_is_sent_to_the_engine_and_followed_by_a_new_image(self):
        engine = FakeEngine()
        c, messages = client([("go_to", {"x": 1.5, "y": 2.0, "stop_short": 0.7}), ("drop", {})])
        with tempfile.TemporaryDirectory() as directory:
            run("Fetch it", c, "claude-sonnet-5-5", engine, directory, 5)
        self.assertIn(("/go_to", {"x": 1.5, "y": 2.0, "stop_short": 0.7}), engine.calls)
        self.assertEqual(sum(path == "/look" for path, _ in engine.calls), 2)
        self.assertIn("go_to", [t["name"] for t in messages.requests[0]["tools"]])

    def test_go_to_without_numbers_is_rejected_before_the_engine(self):
        engine = FakeEngine()
        c, _ = client([("go_to", {"x": "left"}), ("drop", {})])
        with tempfile.TemporaryDirectory() as directory:
            run("Fetch it", c, "claude-sonnet-5-5", engine, directory, 5)
        self.assertFalse(any(path == "/go_to" for path, _ in engine.calls))

    def test_move_and_turn_only_mode_hides_go_to(self):
        engine = FakeEngine()
        c, messages = client([("drop", {})])
        with tempfile.TemporaryDirectory() as directory:
            run("Fetch it", c, "claude-sonnet-5-5", engine, directory, 3, allow_go_to=False)
        request = messages.requests[0]
        self.assertNotIn("go_to", [t["name"] for t in request["tools"]])
        self.assertNotIn("go_to", request["system"][0]["text"])

    def test_replay_page_shows_frames_reasoning_and_tool_calls(self):
        engine = FakeEngine(collide_first_move=True)
        c, _ = client([("move", {"distance": 1}), ("pickup", {}), ("drop", {})])
        with tempfile.TemporaryDirectory() as directory:
            run("Bring the <b>thing</b>", c, "claude-sonnet-5-5", engine, directory, 5)
            page = next(Path(directory).glob("2*/replay.html")).read_text(encoding="utf-8")
        self.assertIn("frame-001.png", page)
        self.assertIn("The target is ahead.", page)
        self.assertIn("collision", page)
        self.assertIn("No new image this step", page)
        self.assertIn("&lt;b&gt;thing&lt;/b&gt;", page)   # command text is escaped
        self.assertNotIn("<b>thing</b>", page)


class SearchNoteTests(unittest.TestCase):
    def test_status_lists_viewed_headings_and_stops_repeat_sweeps(self):
        from agent.run_agent import search_note, status_text
        self.assertIsNone(search_note([0], 1))
        self.assertNotIn("full circle", search_note([0, 90, 180], 1))        # 180-360 still unseen
        full = search_note([0, 45, 90, 135, 180, 225, 270, 315], 1)
        self.assertIn("full circle", full)
        self.assertNotIn("fail", full)
        self.assertIn("call fail", search_note([0, 90, 180, 270.2], 3))      # third spot: allowed to give up
        self.assertIn("Headings already viewed", status_text(3, 2.0, {"viewed": [0, 45], "spots": 1}, True))

import math
import tempfile
import unittest
from types import SimpleNamespace

from agent import scout
from tests.test_agent import tiny_jpeg


LINE = {"target_visible": "TARGET", "target_x": "TARGET_X", "zone_visible": "ZONE", "zone_x": "ZONE_X", "zone_y": "ZONE_Y",
        "zone_under_robot": "ZONE_UNDER_ROBOT", "target_view": "TARGET_VIEW", "zone_view": "ZONE_VIEW", "note": "NOTE"}


class Eyes:
    """Stand-in vision model: answers from a script, one report per call."""

    def __init__(self, reports):
        self.reports, self.requests = list(reports), []
        self.beta = SimpleNamespace(messages=self)

    def create(self, **kwargs):
        self.requests.append(kwargs)
        report = self.reports.pop(0)
        lines = [] if report is None else [f"{LINE[key]}: {'yes' if value is True else 'no' if value is False else value}" for key, value in report.items()]
        return SimpleNamespace(content=[SimpleNamespace(type="text", text=chr(10).join(lines))], stop_reason="end_turn", usage=None)


class World:
    """Stand-in engine: a robot pose, one item on a known bearing, and a drop zone flag."""

    def __init__(self, item_heading=45.0, item_depth=3.0):
        self.x, self.y, self.heading, self.holding, self.done = 0.0, 0.0, 0.0, False, False
        self.item_heading, self.item_depth, self.calls, self.status = item_heading, item_depth, [], []

    def call(self, path, body=None, timeout=30):
        self.calls.append((path, body))
        if path == "/look":
            return tiny_jpeg()
        if path == "/state":
            return {"state": "Done" if self.done else "Locate", "x": self.x, "y": self.y, "heading": self.heading, "holding": self.holding,
                    "retries": 0, "outcome": "pass" if self.done else None}
        if path == "/depth":   # the item is only hit within 2 degrees of its bearing
            off = abs((self.heading - self.item_heading + 180) % 360 - 180)
            return self.item_depth if off <= 2 else None
        if path == "/turn":
            self.heading = (self.heading + body["degrees"]) % 360
        if path == "/go_to":
            self.goal = (body["x"], body["y"], body.get("stop_short", 0))
            return {"ok": True, "reached": True, "collided": False}
        if path == "/pickup":
            self.holding = True
        if path == "/drop":
            self.holding, self.done = False, True
            return {"ok": True, "in_zone": True}
        if path == "/agent_status":
            self.status.append(body["command"])
        return {"ok": True}


class GeometryTests(unittest.TestCase):
    def test_image_position_to_bearing_and_ground_distance(self):
        self.assertAlmostEqual(scout.bearing_from_x(0.5), 0.0)
        self.assertAlmostEqual(scout.bearing_from_x(1.0), 45.0)
        self.assertAlmostEqual(scout.bearing_from_x(0.0), -45.0)
        self.assertIsNone(scout.ground_distance_from_y(0.3))          # above the horizon
        near, far = scout.ground_distance_from_y(1.0), scout.ground_distance_from_y(0.5)
        self.assertAlmostEqual(near, 0.5 / math.tan(math.radians(10 + 36.87)) + 0.28, places=2)
        self.assertGreater(far, near)

    def test_point_ahead_uses_the_engine_heading_convention(self):
        x, y = scout.ahead({"x": 1.0, "y": 1.0, "heading": 90.0}, 2.0)     # heading 90 = toward -y
        self.assertAlmostEqual(x, 1.0)
        self.assertAlmostEqual(y, -1.0)


class ScoutRunTests(unittest.TestCase):
    def run_scout(self, reports, world=None, sheet=True):
        world = world or World()
        eyes = Eyes(reports)
        with tempfile.TemporaryDirectory() as d:
            summary = scout.Scout(eyes, "m", world, runs_dir=d, say=lambda line: None, use_sheet=sheet).run("Bring the red cube")
        return summary, world, eyes

    def test_full_fetch_with_a_handful_of_model_calls(self):
        summary, world, eyes = self.run_scout([
            {"target_view": 2, "zone_view": 1, "note": "view 2 shows it"},                     # sheet: item in view 2 (heading 45), zone in view 1
            {"target_visible": True, "target_x": 0.55},                                        # close look: item just right of centre
            {"target_visible": True, "target_x": 0.5},                                         # check before pickup: it is the right thing
            {"zone_visible": True, "zone_x": 0.5, "zone_y": 0.62},                             # carrying: zone ahead
            {"zone_visible": True, "zone_under_robot": True},
        ])
        self.assertEqual((summary["outcome"], summary["model_calls"]), ("pass", 5))   # sheet, close look, check, two drop-zone looks
        gx, gy, stop = world.goal_first if hasattr(world, "goal_first") else world.goal
        tools = [p for p, _ in world.calls]
        self.assertIn("/pickup", tools)
        self.assertIn("/drop", tools)
        self.assertLess(tools.index("/pickup"), tools.index("/drop"))
        self.assertTrue(any("Done" in s for s in world.status))

    def test_depth_sweep_aims_at_the_item_and_go_to_targets_its_centre(self):
        world = World(item_heading=48.0, item_depth=3.0)
        eyes = Eyes([{"target_view": 1, "zone_view": 0}, {"target_visible": True, "target_x": 0.5}, {"target_visible": True, "target_x": 0.5},
                     {"zone_visible": True, "zone_under_robot": True}])
        world.heading = 45.0
        goals = []
        original = world.call

        def spy(path, body=None, timeout=30):
            if path == "/go_to":
                goals.append((body, world.heading))
            return original(path, body, timeout)
        world.call = spy
        with tempfile.TemporaryDirectory() as d:
            scout.Scout(eyes, "m", world, runs_dir=d, say=lambda line: None, use_sheet=True).run("Bring it")
        body, heading = goals[0]
        self.assertAlmostEqual(heading, 48.0, delta=1.6)                    # swept back to the bearing that hit
        reach = 0.28 + 3.0 + 0.12 + 0.2
        self.assertAlmostEqual(body["x"], reach * math.cos(math.radians(heading)), delta=0.02)
        self.assertAlmostEqual(body["y"], -reach * math.sin(math.radians(heading)), delta=0.02)
        self.assertEqual(body["stop_short"], 0.76)

    def test_sheet_is_one_numbered_image_of_eight_views(self):
        import io
        from PIL import Image
        sheet = Image.open(io.BytesIO(scout.make_sheet([tiny_jpeg()] * 8)))
        self.assertEqual(sheet.size, (1920, 720))

    def test_sweep_picks_the_thing_on_the_bearing_not_a_nearer_neighbour(self):
        # A nearer thing on the left (-9..-4.5 deg at 3.2 m), the wanted one in the middle (-1.5..3 deg at 3.6 m), wall behind.
        readings = []
        for i in range(17):
            offset = -12 + i * 1.5
            depth = 3.2 if -9 <= offset <= -4.5 else 3.6 if -1.5 <= offset <= 3 else 7.0
            readings.append((offset, depth))
        offset, depth = scout.pick_segment(readings)
        self.assertAlmostEqual(depth, 3.6)
        self.assertAlmostEqual(offset, 0.75, delta=0.01)
        self.assertIsNone(scout.pick_segment([(0.0, None), (1.5, 9.5)]))

    def test_wrong_thing_in_front_is_caught_before_pickup(self):
        summary, world, _ = self.run_scout([
            {"target_view": 2}, {"target_visible": True, "target_x": 0.5},
            {"target_visible": False},                       # check: this is not it
            {"target_visible": False},                       # re-aim: lost it
        ])
        self.assertEqual(summary["outcome"], "fail_not_reached")
        self.assertNotIn("/pickup", [p for p, _ in world.calls])

    def test_answers_are_parsed_leniently(self):
        text = "**NOTE:** A large red cube is directly ahead." + chr(10) + "TARGET: Yes" + chr(10) + "- TARGET_X: 0.48 (slightly left)" + chr(10) + "ZONE = no" + chr(10) + "junk line"
        self.assertEqual(scout.parse_answer(text), {"note": "A large red cube is directly ahead.", "target_visible": True, "target_x": 0.48, "zone_visible": False})

    def test_missing_answer_is_asked_again_once(self):
        summary, _, eyes = self.run_scout([
            {"note": "red cube ahead"},                               # forgot TARGET: asked again
            {"target_visible": True, "target_x": 0.5},
            {"target_visible": True, "target_x": 0.5},                # check before pickup
            {"zone_visible": True, "zone_under_robot": True},
        ], World(item_heading=0.0), sheet=False)
        self.assertEqual((summary["outcome"], summary["model_calls"]), ("pass", 4))

    def test_small_models_skip_the_sheet_by_default(self):
        self.assertFalse(scout.Scout(None, "qwen/qwen3.8-27b", World()).use_sheet)
        self.assertTrue(scout.Scout(None, "claude-sonnet-5-5", World()).use_sheet)

    def test_nothing_matching_ends_cleanly(self):
        summary, world, eyes = self.run_scout([{"target_view": 0}] + [{"target_visible": False}] * 9)
        self.assertEqual(summary["outcome"], "fail_not_reached")
        self.assertIn("could not see", summary["problem"])
        self.assertIn(("/fail", {}), world.calls)

    def test_only_the_camera_image_and_the_request_reach_the_model(self):
        world = World(item_heading=0.0)
        _, _, eyes = self.run_scout([{"target_view": 1}, {"target_visible": True, "target_x": 0.5}, {"target_visible": True, "target_x": 0.5},
                                     {"zone_visible": True, "zone_under_robot": True}], world)
        text = ""
        for request in eyes.requests:
            content = request["messages"][0]["content"]
            self.assertEqual([b["type"] for b in content], ["image", "text"])
            text += (request["system"][0]["text"] + content[1]["text"].replace("Bring the red cube", "")).lower()
        for word in ("red", "blue", "yellow", "green", "cube", "sphere", "cylinder", "cone", "warehouse", "pallet"):
            self.assertNotRegex(text, rf"\b{word}\b")


if __name__ == "__main__":
    unittest.main()


class VoicePathTests(unittest.TestCase):
    def test_recording_is_fetched_transcribed_shown_and_returned(self):
        import agent.transcribe as transcribe_module

        class AudioWorld(World):
            def call(self, path, body=None, timeout=30):
                if path == "/agent_audio":
                    self.calls.append((path, body))
                    return b"RIFF....WAVE"
                return super().call(path, body, timeout)

        world, original = AudioWorld(), transcribe_module.transcribe
        transcribe_module.transcribe = lambda wav, say=print: "Bring me the tallest object." if wav.startswith(b"RIFF") else ""
        try:
            s = scout.Scout(Eyes([]), "m", world, say=lambda line: None)
            text = scout.voice_command(s)
        finally:
            transcribe_module.transcribe = original
        self.assertEqual(text, "Bring me the tallest object.")
        self.assertIn(("/agent_heard", {"command": "Bring me the tallest object."}), world.calls)

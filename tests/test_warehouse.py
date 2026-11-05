"""Warehouse scene (engine/Warehouse.unity): rule-compliance checks that need no running engine.

They read the engine source and drive the agent with stand-ins, so they use no model tokens.
"""

import io
import json
import re
import tempfile
import unittest
import urllib.error
from pathlib import Path

from agent.groq_client import to_openai
from agent.run_agent import LIFT_TOOLS, SYSTEM_LIFT, build_messages, request_kwargs, run
from tests.test_agent import client, tiny_jpeg

SCRIPTS = Path(__file__).resolve().parents[1] / "engine" / "Assets" / "Scripts"
API = (SCRIPTS / "WarehouseApi.cs").read_text(encoding="utf-8")
DEMO = (SCRIPTS / "WarehouseDemo.cs").read_text(encoding="utf-8")

# Anything that would tell the model what is in the scene.
SCENE_WORDS = ("red", "blue", "yellow", "brown", "green", "crate", "carton", "box", "boxes", "cube", "pallet", "pallets",
               "conveyor", "belt", "warehouse", "shelf", "rack")


def class_fields(source, name):
    body = re.search(r"class " + name + r"\s*\{(.*?)\n    \}", source, re.S).group(1)
    names = []
    for declaration in re.findall(r"public\s+\w+\s+([^;]+);", body):
        names += [n.strip() for n in declaration.split(",")]
    return set(names)


class WarehouseEngine:
    """Stand-in for the warehouse API: no /view (so the agent picks the lift robot), runs that need two deliveries."""

    def __init__(self, deliveries_needed=1):
        self.calls, self.needed, self.delivered, self.holding = [], deliveries_needed, 0, False

    def call(self, path, body=None, timeout=30):
        self.calls.append((path, body))
        if path == "/view":
            raise urllib.error.HTTPError("x", 404, "not found", {}, io.BytesIO(b'{"error":"Unknown endpoint"}'))
        if path == "/look":
            return tiny_jpeg()
        if path == "/depth":
            return 2.88
        if path == "/pickup":
            self.holding = True
        if path == "/drop":
            self.holding = False
            self.delivered += 1
        if path == "/state":
            done = self.delivered >= self.needed
            return {"state": "Done" if done else "Locate", "holding": self.holding, "retry_count": 0, "x": 0.0, "y": 0.0, "heading": 0.0,
                    "outcome": "pass" if done else None}
        return {"ok": True, "in_zone": path == "/drop"}


class EngineSourceAudit(unittest.TestCase):
    def test_state_and_action_replies_carry_robot_data_only(self):
        self.assertEqual(class_fields(API, "StateOutput"), {"name", "state", "outcome", "x", "y", "heading", "holding", "retry_count", "retries"})
        self.assertEqual(class_fields(API, "ActionOutput"), {"success", "collided", "in_zone", "ok", "moved", "heading"})

    def test_error_messages_say_nothing_about_the_scene_or_the_target(self):
        messages = re.findall(r'Error\(context, \d+, "([^"]+)"\)', API)
        self.assertGreaterEqual(len(messages), 4)
        for message in messages:
            self.assertNotIn("target", message.lower())
            for word in SCENE_WORDS:
                self.assertIsNone(re.search(rf"\b{word}\b", message.lower()), message)

    def test_every_command_is_accepted(self):
        self.assertNotIn("does not identify a supported target", API)
        start = API[API.index('path == "/start"'):API.index('path == "/move"')]
        self.assertEqual(start.count("Error(context"), 2)  # only: empty command, run already started

    def test_camera_contract(self):
        self.assertIn("new Vector3(0f, 0.5f, 0.58f)", API)          # 0.5 m above the floor, at the front
        self.assertIn("Quaternion.Euler(10f, 0f, 0f)", API)         # tilted 10 degrees down
        self.assertIn("fieldOfView = 73.74f", API)                  # 90 degrees horizontal at 4:3
        self.assertIn("new RenderTexture(640, 480", API)            # 640 x 480
        self.assertIn("EncodeToJPG", API)

    def test_motion_collision_and_clock_rules(self):
        self.assertIn("Mathf.Min(remaining, Time.deltaTime)", API)            # 1 m/s
        self.assertIn("Time.deltaTime * 90f", API)                            # 90 degrees per second
        self.assertIn("direction * 0.1f", API)                                # backs off 0.1 m
        self.assertIn('apiRetries >= 3 ? "Done" : "Blocked"', API)            # Blocked, then Done after 3
        self.assertIn('apiOutcome = "fail_collision"', API)
        self.assertIn("apiMode && !apiBusy) ? 0f", DEMO)                      # world frozen while the agent thinks
        for outcome in ("pass", "fail_wrong_object", "fail_not_reached", "fail_collision"):
            self.assertIn('"' + outcome + '"', API)

    def test_cargo_has_shared_shapes_and_shared_colours_inside_a_walled_room(self):
        self.assertIn('{ "crate", "flat carton", "tall carton", "box" }', DEMO)
        self.assertIn('{ "red", "blue", "yellow", "brown", "" }', DEMO)
        self.assertIn("cargoMats[kind, color]", DEMO)
        self.assertEqual(DEMO.count("        Wall(new Vector3("), 4)
        self.assertIn("surface == beltMat ? zoneMat : orange", DEMO)          # drop zone is marked


class LiftAgentTests(unittest.TestCase):
    def test_agent_detects_the_lift_robot_and_uses_its_prompt_and_tools(self):
        engine = WarehouseEngine()
        c, messages = client([("pickup", {}), ("drop", {})])
        with tempfile.TemporaryDirectory() as directory:
            summary = run("Bring the red crate", c, "claude-sonnet-5-5", engine, directory, 6)
        request = messages.requests[0]
        self.assertEqual(summary["robot"], "lift")
        self.assertEqual(request["system"][0]["text"], SYSTEM_LIFT)
        self.assertEqual([t["name"] for t in request["tools"]], ["move", "turn", "pickup", "drop", "fail"])
        self.assertFalse(any(path == "/go_to" for path, _ in engine.calls))

    def test_a_command_for_two_items_keeps_going_until_both_are_delivered(self):
        engine = WarehouseEngine(deliveries_needed=2)
        c, _ = client([("pickup", {}), ("drop", {}), ("turn", {"degrees": 90}), ("pickup", {}), ("drop", {})])
        with tempfile.TemporaryDirectory() as directory:
            summary = run("Bring two red crates", c, "claude-sonnet-5-5", engine, directory, 10)
        self.assertEqual((summary["outcome"], summary["step_count"]), ("pass", 5))
        self.assertEqual(sum(path == "/drop" for path, _ in engine.calls), 2)

    def test_prompt_tools_and_free_model_payload_describe_no_scene(self):
        static = (SYSTEM_LIFT + json.dumps(LIFT_TOOLS)).lower()
        messages = build_messages("Fetch it", tiny_jpeg(), 2.88, {"holding": False, "retry_count": 0, "x": 0.0, "y": 0.0, "heading": 0.0}, [], 1, None)
        body = to_openai(request_kwargs("qwen/qwen3.8-27b", messages, LIFT_TOOLS, SYSTEM_LIFT))
        text = static
        for message in body["messages"]:
            parts = message["content"] if isinstance(message["content"], list) else [{"type": "text", "text": message["content"] or ""}]
            text += " " + " ".join(p.get("text", "") for p in parts if p["type"] == "text").lower()
        text += " " + json.dumps(body["tools"]).lower()
        for word in SCENE_WORDS:
            self.assertIsNone(re.search(rf"\b{word}\b", text), word)
        images = [p for m in body["messages"] if isinstance(m["content"], list) for p in m["content"] if p["type"] == "image_url"]
        self.assertEqual(len(images), 1)   # the camera frame is the only scene information


if __name__ == "__main__":
    unittest.main()

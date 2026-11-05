import io
import json
import tempfile
import unittest
import urllib.error
from types import SimpleNamespace

from agent import quick


class PlanClient:
    def __init__(self, steps=None, note="ok", text=""):
        self.steps, self.note, self.text, self.requests = steps, note, text, []
        self.beta = SimpleNamespace(messages=self)

    def create(self, **kwargs):
        self.requests.append(kwargs)
        content = [SimpleNamespace(type="text", text=self.text)] if self.text else []
        if self.steps is not None:
            content.append(SimpleNamespace(type="tool_use", id="t1", name="plan", input={"steps": self.steps, "note": self.note}))
        return SimpleNamespace(content=content, stop_reason="tool_use", usage=None)


class QuickEngine:
    def __init__(self, delivered=()):
        self.calls, self.delivered, self.done = [], set(delivered), False

    def call(self, path, body=None, timeout=30):
        self.calls.append((path, body))
        if path == "/run_builtin":
            name = body["command"].replace("Bring the ", "")
            if name in self.delivered:
                raise urllib.error.HTTPError("x", 409, "conflict", {}, io.BytesIO(b'{"error":"Already delivered; reset first"}'))
            self.delivered.add(name)
            self.done = True
        if path == "/state":
            return {"state": "Done" if self.done else "Idle", "outcome": "pass"}
        return {"ok": True}


class QuickTests(unittest.TestCase):
    def test_request_becomes_basic_commands_and_runs_them_in_order(self):
        client, engine = PlanClient(["dark mode on", "fetch yellow cylinder", "camera top-down"], "The cylinder is the drink-shaped one."), QuickEngine()
        with tempfile.TemporaryDirectory() as d:
            record = quick.handle("turn off the lights and bring me something to drink, view from above", client, "m", engine, runs_dir=d, say=lambda s: None)
        paths = [p for p, _ in engine.calls if p != "/state"]
        self.assertEqual(paths, ["/dark", "/run_builtin", "/view"])
        self.assertEqual(engine.calls[0][1], {"on": True})
        self.assertEqual([r["result"] for r in record["results"]], ["done", "pass", "done"])

    def test_model_gets_app_context_and_only_text(self):
        client = PlanClient(["fetch red cube"])
        quick.interpret("bring the read cube", client, "m")
        request = client.requests[0]
        self.assertIn("yellow cylinder: the tallest object", request["system"][0]["text"])
        self.assertIn("mishear", request["system"][0]["text"])
        self.assertNotIn('"image"', json.dumps(request["messages"]))
        self.assertEqual(request["tools"][0]["input_schema"]["properties"]["steps"]["items"]["enum"], quick.COMMANDS)

    def test_unknown_and_repeated_steps_are_dropped(self):
        self.assertEqual(quick.clean_steps(["fetch red cube", "launch rocket", "Fetch Red Cube", "reset scene"]), ["fetch red cube", "reset scene"])
        self.assertEqual(quick.clean_steps("fetch red cube"), [])

    def test_plain_text_reply_is_still_understood(self):
        client = PlanClient(None, text="I will reset scene and then fetch blue sphere.")
        steps, _ = quick.interpret("start over and get the blue ball", client, "m")
        self.assertEqual(steps, ["reset scene", "fetch blue sphere"])

    def test_already_delivered_object_is_reported_not_crashed(self):
        client, engine = PlanClient(["fetch red cube", "fetch blue cube"]), QuickEngine(delivered={"red cube"})
        with tempfile.TemporaryDirectory() as d:
            record = quick.handle("both cubes", client, "m", engine, runs_dir=d, say=lambda s: None)
        self.assertIn("Already delivered", record["results"][0]["result"])
        self.assertEqual(record["results"][1]["result"], "pass")

    def test_impossible_request_runs_nothing(self):
        client, engine = PlanClient([], "The robot cannot make coffee."), QuickEngine()
        with tempfile.TemporaryDirectory() as d:
            record = quick.handle("make me a coffee", client, "m", engine, runs_dir=d, say=lambda s: None)
        self.assertEqual((record["steps"], engine.calls), ([], []))


if __name__ == "__main__":
    unittest.main()

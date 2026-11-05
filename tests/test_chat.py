import json
import tempfile
import threading
import time
import unittest
import urllib.request

from agent import chat
from tests.test_agent import FakeEngine, client
from tests.test_quick import PlanClient, QuickEngine


def wait_idle(agent, timeout=10):
    end = time.time() + timeout
    while time.time() < end:
        if not agent.busy and not agent.jobs:
            time.sleep(0.1)
            if not agent.busy and not agent.jobs:
                return
        time.sleep(0.05)
    raise AssertionError("agent did not finish")


class ChatTests(unittest.TestCase):
    def test_fast_request_runs_and_reports_done(self):
        with tempfile.TemporaryDirectory() as d:
            agent = chat.Agent(PlanClient(["fetch yellow cylinder"], "The cylinder is drink-shaped."), "m", QuickEngine(), d)
            self.assertTrue(agent.ask("bring me something to drink"))
            wait_idle(agent)
        roles = [e["role"] for e in agent.events]
        self.assertEqual(roles[0], "user")
        self.assertIn("step", roles)
        self.assertTrue(agent.events[-1]["good"])
        self.assertIn("Done", agent.events[-1]["text"])

    def test_impossible_request_gets_an_explanation_and_engine_down_is_reported(self):
        with tempfile.TemporaryDirectory() as d:
            agent = chat.Agent(PlanClient([], "The robot cannot make coffee."), "m", QuickEngine(), d)
            agent.ask("make me a coffee")
            wait_idle(agent)
            self.assertEqual(agent.events[-1]["text"], "The robot cannot make coffee.")

            class DeadEngine:
                def call(self, *a, **k):
                    raise ConnectionRefusedError("[WinError 10061] refused")
            agent = chat.Agent(PlanClient(["fetch red cube"]), "m", DeadEngine(), d)
            agent.ask("bring the red cube")
            wait_idle(agent)
            self.assertIn("Press Play in Unity", agent.events[-1]["text"])
        self.assertFalse(agent.ask("   "))

    def test_camera_mode_streams_steps(self):
        c, _ = client([("turn", {"degrees": 45}), ("drop", {})])
        with tempfile.TemporaryDirectory() as d:
            agent = chat.Agent(c, "claude-sonnet-5-5", FakeEngine(), d)
            agent.ask("Fetch it", "camera")
            wait_idle(agent)
        steps = [e["text"] for e in agent.events if e["role"] == "step"]
        self.assertTrue(steps[0].startswith("1. turn(degrees=45)"))
        self.assertIn("pass", agent.events[-1]["text"])

    def test_http_page_ask_and_events(self):
        with tempfile.TemporaryDirectory() as d:
            agent = chat.Agent(PlanClient(["reset scene"], "Scene reset."), "m", QuickEngine(), d)
            server = chat.serve(agent, 0)
            port = server.server_address[1]
            threading.Thread(target=server.serve_forever, daemon=True).start()
            try:
                base = f"http://127.0.0.1:{port}"
                page = urllib.request.urlopen(base + "/").read().decode()
                self.assertIn("RoboHalo agent", page)
                request = urllib.request.Request(base + "/ask", data=json.dumps({"text": "start over"}).encode(), headers={"Content-Type": "application/json"})
                self.assertTrue(json.loads(urllib.request.urlopen(request).read())["ok"])
                wait_idle(agent)
                data = json.loads(urllib.request.urlopen(base + "/events?since=0").read())
                self.assertEqual(data["events"][0]["text"], "start over")
                self.assertFalse(data["busy"])
                later = json.loads(urllib.request.urlopen(base + f"/events?since={data['events'][-1]['id']}").read())
                self.assertEqual(later["events"], [])
            finally:
                server.shutdown()


if __name__ == "__main__":
    unittest.main()

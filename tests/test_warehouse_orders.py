"""Warehouse Operations must wait for requests and select only requested cargo."""

import unittest
from pathlib import Path

from agent.warehouse_orders import interpret, preset


DEMO = (Path(__file__).resolve().parents[1] / "engine/Assets/Scripts/WarehouseDemo.cs").read_text(encoding="utf-8")


class WarehouseOrderTests(unittest.TestCase):
    def test_explicit_requests_use_no_model_call(self):
        def unavailable(_):
            raise AssertionError("The preset should save the API call")

        self.assertEqual(interpret("Please fetch two small blue boxes", unavailable),
                         {"command": "fetch 2 blue flat carton", "source": "preset"})
        self.assertEqual(interpret("Bring 3 large red boxes", unavailable)["command"],
                         "fetch 3 red tall carton")

    def test_indirect_request_uses_model_and_failure_does_not_guess(self):
        self.assertEqual(interpret("Fetch the one suitable for oversized goods", lambda _: {"command": "fetch 1 yellow tall carton", "source": "groq"})["command"],
                         "fetch 1 yellow tall carton")
        self.assertIn("error", interpret("What is happening?", lambda _: {"error": "No fetch request"}))
        self.assertIn("error", interpret("Pick something unfamiliar", lambda _: (_ for _ in ()).throw(RuntimeError("offline"))))

    def test_scene_does_not_autoload_or_take_unrequested_stock(self):
        start = DEMO[DEMO.index("private void Start()"):DEMO.index("// Conveyor:")]
        self.assertNotIn("ImportCsv();", start)
        load = DEMO[DEMO.index("private IEnumerator Load("):DEMO.index("private IEnumerator Restock()")]
        self.assertIn('"Waiting for an Operations request"', load)
        self.assertNotIn("if (wanted.Count == 0) wanted = open", load)
        self.assertIn("orderLeft--; orderInFlight++", load)
        self.assertIn('PlaceOrder(text, "local preset fallback")', DEMO)


if __name__ == "__main__":
    unittest.main()

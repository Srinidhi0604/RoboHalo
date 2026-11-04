import unittest

from agent.manual_agent import TARGETS, build_plan, resolve_target


class ManualAgentTests(unittest.TestCase):
    def test_all_six_round_one_objects_have_plans(self):
        self.assertEqual(len(TARGETS), 6)
        for name in TARGETS:
            plan = build_plan("Bring the " + name)
            self.assertEqual(plan[-1][0], "drop")
            self.assertIn(("pickup", {}), plan)
            self.assertTrue(all(abs(args["distance"]) <= 1 for tool, args in plan if tool == "move"))
            self.assertTrue(all(abs(args["degrees"]) <= 180 for tool, args in plan if tool == "turn"))

    def test_reasoning_commands(self):
        self.assertEqual(resolve_target("Bring the round red thing"), "red sphere")
        self.assertEqual(resolve_target("Bring the tallest object"), "yellow cylinder")


if __name__ == "__main__":
    unittest.main()

import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from agent import hidden_runs, membrane
from agent.clip import FPS, build_clip
from tests.test_agent import FakeEngine, client
from tests.test_clip import write_frames


def make_clip(directory, name="clip.mp4"):
    frames = Path(directory) / "frames"
    frames.mkdir()
    write_frames(frames, FPS)
    out = Path(directory) / name
    build_clip(frames, "Bring the red cube", out)
    return out


class FakeTransport:
    def __init__(self, fail_times=0):
        self.uploads, self.fail_times = [], fail_times

    def upload(self, clip_path, metadata, idempotency_key):
        if self.fail_times:
            self.fail_times -= 1
            raise membrane.TransientError("503")
        self.uploads.append(idempotency_key)
        return f"sub-{len(self.uploads)}"

    def results(self, submission_id):
        raw = {"anything": "as the service sends it"}
        return raw, {"pulse1": ["yes"] * 24 + ["no"] * 6, "pulse2": ["nothing wrong"] * 20 + ["hit something"] * 7 + ["too slow"] * 3}


class MembraneTests(unittest.TestCase):
    def test_key_from_env_file_and_masking(self):
        with tempfile.TemporaryDirectory() as d:
            env = Path(d) / ".membrane.env"
            env.write_text('MEMBRANE_API_KEY="mk-1234567890abcdef"\n')
            old = os.environ.pop("MEMBRANE_API_KEY", None)
            try:
                key = membrane.load_api_key(env)
            finally:
                if old is not None:
                    os.environ["MEMBRANE_API_KEY"] = old
            self.assertEqual(key, "mk-1234567890abcdef")
            self.assertNotIn("567890", membrane.mask(key))

    def test_invalid_clip_is_refused_before_any_upload(self):
        transport = FakeTransport()
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(ValueError):
                membrane.submit(Path(d) / "missing.mp4", {}, transport, Path(d) / "ledger.json")
        self.assertEqual(transport.uploads, [])

    def test_same_clip_is_never_submitted_twice_and_transients_retry(self):
        transport = FakeTransport(fail_times=2)
        with tempfile.TemporaryDirectory() as d:
            clip, ledger = make_clip(d), Path(d) / "ledger.json"
            first = membrane.submit(clip, {"command": "x"}, transport, ledger, sleep=lambda s: None)
            second = membrane.submit(clip, {"command": "x"}, transport, ledger, sleep=lambda s: None)
            self.assertEqual((first["status"], first["submission_id"]), ("submitted", "sub-1"))
            self.assertEqual(second["submission_id"], "sub-1")
            self.assertEqual(len(transport.uploads), 1)

    def test_unwired_official_transport_records_pending_not_crash(self):
        with tempfile.TemporaryDirectory() as d:
            entry = membrane.submit(make_clip(d), {}, membrane.OfficialTransport("k"), Path(d) / "ledger.json")
            self.assertTrue(entry["status"].startswith("pending"))
            self.assertIsNone(entry["submission_id"])

    def test_results_saved_raw_and_summarised(self):
        with tempfile.TemporaryDirectory() as d:
            summary = membrane.fetch_results({"submission_id": "sub-9"}, FakeTransport(), Path(d))
            self.assertEqual(json.loads((Path(d) / "sub-9.raw.json").read_text()), {"anything": "as the service sends it"})
            self.assertEqual(summary["pulse1_yes_rate"], 0.8)
            self.assertEqual(summary["pulse2_split"]["hit something"], 7)
            self.assertAlmostEqual(summary["nothing_wrong_share"], 0.667)
            self.assertTrue(summary["enough_responses"])
            self.assertTrue(membrane.compare("pass", summary)["agrees"])
            self.assertFalse(membrane.compare("fail_collision", summary)["agrees"])


class HiddenRunbookTests(unittest.TestCase):
    def test_record_once_run_once_and_checklist(self):
        with tempfile.TemporaryDirectory() as d:
            base = Path(d) / "hidden"
            hidden_runs.record(["Bring me something to drink", "Fetch the tallest thing", "Bring the blue one"], base)
            with self.assertRaises(SystemExit):
                hidden_runs.record(["a", "b", "c"], base)  # commands are frozen
            c, _ = client([("drop", {})])
            item = hidden_runs.run_one(1, c, "claude-sonnet-5-5", FakeEngine(), base)
            self.assertEqual(item["outcome"], "pass")
            self.assertTrue(Path(item["run_dir"]).exists())
            with self.assertRaises(SystemExit):
                hidden_runs.run_one(1, c, "claude-sonnet-5-5", FakeEngine(), base)  # exactly once
            text = (base / "checklist.md").read_text()
            self.assertIn("Bring me something to drink", text)
            self.assertIn("not run", text)

    def test_upload_skips_invalid_clips_and_records_ids(self):
        with tempfile.TemporaryDirectory() as d:
            base = Path(d) / "hidden"
            hidden_runs.record(["one", "two", "three"], base)
            checklist = json.loads((base / "checklist.json").read_text())
            checklist["commands"][0]["clip"] = {"ok": True, "path": str(make_clip(d))}
            checklist["commands"][1]["clip"] = {"ok": False, "problems": ["duration"]}
            (base / "checklist.json").write_text(json.dumps(checklist))
            result = hidden_runs.upload_all(FakeTransport(), base, Path(d) / "ledger.json")
            states = [c.get("upload_status") for c in result["commands"]]
            self.assertEqual(states[0], "submitted")
            self.assertTrue(states[1].startswith("skipped"))
            self.assertEqual(result["commands"][0]["submission_id"], "sub-1")


if __name__ == "__main__":
    unittest.main()

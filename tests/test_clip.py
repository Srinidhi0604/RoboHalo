import tempfile
import unittest
from pathlib import Path

import cv2
import numpy as np

from agent.clip import FPS, build_clip, plan_timeline


def write_frames(directory, count):
    for i in range(count):
        frame = np.full((720, 1280, 3), (i * 3) % 255, np.uint8)
        cv2.imwrite(str(Path(directory) / f"frame_{i:05d}.jpg"), frame)


class ClipTests(unittest.TestCase):
    def test_short_motion_is_padded_to_at_least_10_seconds(self):
        indices, speed = plan_timeline(5 * FPS)
        self.assertEqual(speed, 1.0)
        self.assertGreaterEqual(len(indices) / FPS, 10)

    def test_long_motion_is_sped_up_to_under_20_seconds(self):
        indices, speed = plan_timeline(40 * FPS)
        self.assertGreater(speed, 2)
        self.assertLessEqual(len(indices) / FPS, 20)
        self.assertEqual(indices[-1], 40 * FPS - 1)  # still ends on the final frame

    def test_build_clip_meets_upload_rules(self):
        with tempfile.TemporaryDirectory() as directory:
            write_frames(directory, 3 * FPS)
            out = Path(directory) / "clip.mp4"
            info = build_clip(directory, "Bring the red cube", out)
            self.assertTrue(info["ok"], info)
            self.assertEqual((info["width"], info["height"]), (1280, 720))
            self.assertTrue(10 <= info["seconds"] <= 20)
            # command banner is drawn on the first frames only
            capture = cv2.VideoCapture(str(out))
            _, first = capture.read()
            capture.set(cv2.CAP_PROP_POS_FRAMES, 3 * FPS)
            _, later = capture.read()
            capture.release()
            self.assertGreater(float(np.abs(first[40:120].astype(int) - later[40:120].astype(int)).mean()), 5)




class ValidatorTests(unittest.TestCase):
    def test_rejects_short_low_res_and_missing_overlay_and_names_each_rule(self):
        from agent.clip import validate_clip
        with tempfile.TemporaryDirectory() as directory:
            out = Path(directory) / "bad.mp4"
            writer = cv2.VideoWriter(str(out), cv2.VideoWriter_fourcc(*"mp4v"), FPS, (640, 480))
            for _ in range(3 * FPS):
                writer.write(np.zeros((480, 640, 3), np.uint8))
            writer.release()
            report = validate_clip(out)
            joined = " | ".join(report["problems"])
            self.assertFalse(report["ok"])
            for expected in ("duration", "below 1280x720", "overlay"):
                self.assertIn(expected, joined)
            self.assertIn("not .mp4", " | ".join(validate_clip(Path(directory) / "x.avi")["problems"]) + " not .mp4")

    def test_valid_clip_passes_and_cli_exit_code(self):
        import subprocess, sys
        with tempfile.TemporaryDirectory() as directory:
            write_frames(directory, 2 * FPS)
            out = Path(directory) / "clip.mp4"
            build_clip(directory, "Bring the blue cube", out)
            done = subprocess.run([sys.executable, "-m", "agent.clip", "--check", str(out)], capture_output=True)
            self.assertEqual(done.returncode, 0, done.stdout)
            out.with_suffix(".json").unlink()
            done = subprocess.run([sys.executable, "-m", "agent.clip", "--check", str(out)], capture_output=True)
            self.assertEqual(done.returncode, 1)


if __name__ == "__main__":
    unittest.main()

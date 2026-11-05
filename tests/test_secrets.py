"""No API keys in the repository, the logs, or the run artifacts (implementation_spec B2)."""

import os
import re
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PATTERNS = [re.compile(rb"sk-ant-[A-Za-z0-9_\-]{12,}"), re.compile(rb"(?i)(api[_-]?key|token)\s*[:=]\s*[\"'][A-Za-z0-9_\-]{20,}[\"']")]
TEXT_SUFFIXES = {".py", ".md", ".json", ".jsonl", ".txt", ".cs", ".ps1", ".yml", ".yaml", ".toml", ".cfg", ".env", ".log", ""}


def candidate_files():
    listed = subprocess.run(["git", "ls-files", "-co", "--exclude-standard"], cwd=ROOT, capture_output=True, text=True).stdout.split("\n")
    files = {ROOT / f for f in listed if f}
    files |= set((ROOT / "runs").rglob("*.json")) | set((ROOT / "runs").rglob("*.jsonl"))  # ignored, but still artifacts
    return [f for f in files if f.is_file() and f.suffix.lower() in TEXT_SUFFIXES]


class SecretScan(unittest.TestCase):
    def test_no_keys_in_files_logs_or_runs(self):
        live_keys = [v.encode() for v in (os.getenv("ANTHROPIC_API_KEY"), os.getenv("MEMBRANE_API_KEY")) if v and len(v) > 8]
        for path in candidate_files():
            data = path.read_bytes()
            for pattern in PATTERNS:
                self.assertIsNone(pattern.search(data), f"possible key in {path}")
            for key in live_keys:
                self.assertNotIn(key, data, f"live key value found in {path}")

    def test_credential_files_are_git_ignored(self):
        for name in (".env", ".membrane.env"):
            ignored = subprocess.run(["git", "check-ignore", "-q", name], cwd=ROOT).returncode == 0
            self.assertTrue(ignored, f"{name} must be git-ignored")

    def test_no_keys_in_git_history(self):
        log = subprocess.run(["git", "log", "-p", "--all"], cwd=ROOT, capture_output=True).stdout
        self.assertIsNone(PATTERNS[0].search(log), "an Anthropic key appears in git history")


if __name__ == "__main__":
    unittest.main()

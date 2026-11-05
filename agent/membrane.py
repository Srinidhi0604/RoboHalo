"""Membrane human-feedback client: everything except the HTTP calls.

The official API guide (endpoints, headers, schemas, pulse ids) arrives with the key; until then
`OfficialTransport` refuses to run instead of guessing. Everything around it is built and tested:

- credentials from the MEMBRANE_API_KEY env var or an ignored `.membrane.env` file, never logged
- clips are validated before upload; invalid clips are refused with the failed rules
- a local ledger keyed by the clip's SHA-256 prevents duplicate submissions, including across retries
- transient failures are retried with backoff
- raw results are saved untouched; summaries give the pulse 1 yes-rate and the pulse 2 label split
- our own outcome is compared with the crowd verdict

Usage:
  python -m agent.membrane status                 # ledger: what was submitted, ids and states
  python -m agent.membrane summarize results.json # summarise a saved raw result (normalised form)
"""

import hashlib
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LEDGER = ROOT / "runs" / "membrane_ledger.json"
RESULTS_DIR = ROOT / "runs" / "membrane"
PULSE2_LABELS = ("hit something", "wrong object", "did not reach the drop zone", "too slow", "nothing wrong")
OUTCOME_TO_LABEL = {
    "pass": "nothing wrong",
    "fail_wrong_object": "wrong object",
    "fail_not_reached": "did not reach the drop zone",
    "fail_collision": "hit something",
}


class TransientError(RuntimeError):
    """Raised by a transport for failures worth retrying (timeouts, 429, 5xx)."""


class NotWiredError(RuntimeError):
    pass


def load_api_key(env_file=ROOT / ".membrane.env"):
    """MEMBRANE_API_KEY from the environment, else from the git-ignored .membrane.env (KEY=VALUE)."""
    key = os.getenv("MEMBRANE_API_KEY")
    if not key and Path(env_file).exists():
        for line in Path(env_file).read_text(encoding="utf-8").splitlines():
            name, _, value = line.partition("=")
            if name.strip() == "MEMBRANE_API_KEY":
                key = value.strip().strip('"').strip("'")
    return key or None


def mask(secret):
    return "<none>" if not secret else secret[:4] + "..." + f"({len(secret)} chars)"


class OfficialTransport:
    """Fill in from the official Membrane API guide. Do not guess paths, headers or schemas.

    upload(clip_path, metadata, idempotency_key) -> submission id (str)
    status(submission_id) -> state string
    results(submission_id) -> (raw_json, {"pulse1": ["yes"|"no", ...], "pulse2": [label, ...]})
    Raise TransientError for retryable failures.
    """

    def __init__(self, api_key):
        self.api_key = api_key

    def _not_wired(self):
        raise NotWiredError("Membrane API not wired yet: add endpoints from the official guide in agent/membrane.py")

    def upload(self, clip_path, metadata, idempotency_key):
        self._not_wired()

    def status(self, submission_id):
        self._not_wired()

    def results(self, submission_id):
        self._not_wired()


def sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def read_ledger(path=LEDGER):
    return json.loads(Path(path).read_text(encoding="utf-8")) if Path(path).exists() else {}


def write_ledger(ledger, path=LEDGER):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    tmp = Path(path).with_suffix(".tmp")
    tmp.write_text(json.dumps(ledger, indent=2), encoding="utf-8")
    tmp.replace(path)  # atomic: a crash never leaves a half-written ledger


def with_retries(call, attempts=3, base_delay=2.0, sleep=time.sleep):
    for attempt in range(attempts):
        try:
            return call()
        except TransientError:
            if attempt == attempts - 1:
                raise
            sleep(base_delay * (2 ** attempt))


def submit(clip_path, metadata, transport, ledger_path=LEDGER, sleep=time.sleep):
    """Validate, then upload once. Returns the ledger entry. Re-submitting the same clip is a no-op."""
    from agent.clip import validate_clip
    report = validate_clip(clip_path)
    if not report["ok"]:
        raise ValueError("clip rejected: " + "; ".join(report["problems"]))
    key = sha256(clip_path)
    ledger = read_ledger(ledger_path)
    entry = ledger.get(key)
    if entry and entry.get("submission_id"):
        return entry  # already uploaded: never send a duplicate
    entry = {"clip": str(clip_path), "sha256": key, "metadata": metadata, "submission_id": None,
             "status": "uploading", "attempted_at": datetime.now(timezone.utc).isoformat()}
    ledger[key] = entry
    write_ledger(ledger, ledger_path)  # record the attempt before the call, so a crash is visible
    try:
        # The clip hash doubles as the idempotency key, so a retried upload is recognisable server-side.
        entry["submission_id"] = with_retries(lambda: transport.upload(clip_path, metadata, key), sleep=sleep)
        entry["status"] = "submitted"
    except NotWiredError as error:
        entry["status"] = "pending: " + str(error)
    except Exception as error:
        entry["status"] = "failed: " + str(error)
    entry["updated_at"] = datetime.now(timezone.utc).isoformat()
    ledger[key] = entry
    write_ledger(ledger, ledger_path)
    return entry


def fetch_results(entry, transport, results_dir=RESULTS_DIR, sleep=time.sleep):
    """Save the raw result sheet untouched and return its summary."""
    raw, normalised = with_retries(lambda: transport.results(entry["submission_id"]), sleep=sleep)
    Path(results_dir).mkdir(parents=True, exist_ok=True)
    (Path(results_dir) / f"{entry['submission_id']}.raw.json").write_text(json.dumps(raw, indent=2), encoding="utf-8")
    summary = summarize(normalised)
    (Path(results_dir) / f"{entry['submission_id']}.summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    return summary


def summarize(normalised):
    """pulse1: list of yes/no answers; pulse2: list of labels -> yes-rate and label split."""
    answers = [str(a).strip().lower() for a in normalised.get("pulse1", [])]
    labels = [str(l).strip().lower() for l in normalised.get("pulse2", [])]
    yes = sum(a == "yes" for a in answers)
    split = {label: labels.count(label) for label in PULSE2_LABELS}
    unknown = [l for l in labels if l not in PULSE2_LABELS]
    return {
        "pulse1_responses": len(answers),
        "pulse1_yes_rate": round(yes / len(answers), 3) if answers else None,
        "pulse2_responses": len(labels),
        "pulse2_split": split,
        "pulse2_share": {k: round(v / len(labels), 3) for k, v in split.items()} if labels else {},
        "nothing_wrong_share": round(split["nothing wrong"] / len(labels), 3) if labels else None,
        "unrecognised_labels": sorted(set(unknown)),
        "enough_responses": len(answers) >= 30,  # the brief promises at least 30 per clip
    }


def compare(outcome, summary):
    """Does the crowd's most common pulse 2 label match what our engine logged?"""
    expected = OUTCOME_TO_LABEL.get(outcome)
    split = summary.get("pulse2_split") or {}
    top = max(split, key=split.get) if any(split.values()) else None
    return {"our_outcome": outcome, "expected_label": expected, "crowd_top_label": top, "agrees": expected == top}


def main(argv):
    if argv[:1] == ["status"]:
        ledger = read_ledger()
        if not ledger:
            print("No submissions yet.")
        for entry in ledger.values():
            print(f"{entry['status']:<12} {entry.get('submission_id') or '-':<20} {entry['clip']}")
        print("API key:", mask(load_api_key()))
    elif argv[:1] == ["summarize"] and len(argv) == 2:
        print(json.dumps(summarize(json.loads(Path(argv[1]).read_text(encoding="utf-8"))), indent=2))
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    main(sys.argv[1:])

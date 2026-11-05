"""Hidden-command runbook (hour 18 -> upload by hour 20).

  python -m agent.hidden_runs record "cmd 1" "cmd 2" "cmd 3"   # save the three commands verbatim (once)
  python -m agent.hidden_runs run 1                            # run command 1 exactly once, with a clip
  python -m agent.hidden_runs upload                           # submit every valid clip (no duplicates)
  python -m agent.hidden_runs status                           # checklist: command, run, outcome, clip, upload

Each command gets one fresh run folder under runs/hidden/. A second `run` of the same command is
refused, as the brief allows one run per hidden command. The checklist is written to
runs/hidden/checklist.json and checklist.md after every step.
Keep the engine at 1x speed and dark mode off for these runs.
"""

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HIDDEN = ROOT / "runs" / "hidden"


def _load(base):
    path = base / "checklist.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None


def _save(base, checklist):
    base.mkdir(parents=True, exist_ok=True)
    (base / "checklist.json").write_text(json.dumps(checklist, indent=2), encoding="utf-8")
    lines = ["# Hidden-command checklist", "", f"Recorded: {checklist['recorded_at']}", "",
             "| # | Command (verbatim) | Run | Outcome | Clip valid | Upload ID | Upload status |",
             "| --- | --- | --- | --- | --- | --- | --- |"]
    for i, item in enumerate(checklist["commands"], 1):
        clip = item.get("clip") or {}
        valid = "-" if not clip else ("yes" if clip.get("ok") else "NO: " + "; ".join(clip.get("problems", [])))
        lines.append(f"| {i} | {item['command']} | {item.get('run_dir') or 'not run'} | {item.get('outcome') or '-'} | "
                     f"{valid} | {item.get('submission_id') or '-'} | {item.get('upload_status') or '-'} |")
    (base / "checklist.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def record(commands, base=HIDDEN):
    if _load(base):
        raise SystemExit("Hidden commands already recorded; they must not change. See runs/hidden/checklist.md")
    if len(commands) != 3:
        raise SystemExit("Expected exactly three hidden commands.")
    checklist = {"recorded_at": datetime.now(timezone.utc).isoformat(),
                 "commands": [{"command": c, "run_dir": None, "outcome": None} for c in commands]}
    _save(base, checklist)
    return checklist


def run_one(number, client, model, engine=None, base=HIDDEN):
    from agent.run_agent import run
    checklist = _load(base)
    if not checklist:
        raise SystemExit("Record the hidden commands first.")
    item = checklist["commands"][number - 1]
    if item.get("run_dir"):
        raise SystemExit(f"Command {number} was already run ({item['run_dir']}); hidden commands run exactly once.")
    item["started_at"] = datetime.now(timezone.utc).isoformat()
    item["run_dir"] = "in progress"
    _save(base, checklist)  # mark it before running, so a crash cannot lead to a second attempt
    summary = run(item["command"], client, model, engine, runs_dir=str(base / f"command-{number}"), record=True)
    item.update({"run_dir": summary["run_dir"], "outcome": summary["outcome"], "clip": summary.get("clip"),
                 "steps": summary["step_count"], "cost_usd": summary["cost_estimate_usd"]})
    _save(base, checklist)
    return item


def upload_all(transport, base=HIDDEN, ledger_path=None):
    from agent import membrane
    checklist = _load(base)
    for i, item in enumerate(checklist["commands"], 1):
        clip = item.get("clip") or {}
        if not clip.get("ok"):
            item["upload_status"] = "skipped: no valid clip"
            continue
        kwargs = {"ledger_path": ledger_path} if ledger_path else {}
        entry = membrane.submit(clip["path"], {"command": item["command"], "hidden_command": i}, transport, **kwargs)
        item["submission_id"] = entry.get("submission_id")
        item["upload_status"] = entry["status"]
    _save(base, checklist)
    return checklist


def main(argv):
    if argv[:1] == ["record"]:
        record(argv[1:])
    elif argv[:1] == ["run"] and len(argv) == 2:
        import os
        from agent.run_agent import DEFAULT_MODEL, claude_client
        print(json.dumps(run_one(int(argv[1]), claude_client(), os.getenv("CLAUDE_MODEL", DEFAULT_MODEL)), indent=2))
    elif argv[:1] == ["upload"]:
        from agent import membrane
        upload_all(membrane.OfficialTransport(membrane.load_api_key()))
    elif argv[:1] != ["status"]:
        sys.exit(__doc__)
    path = HIDDEN / "checklist.md"
    print(path.read_text(encoding="utf-8") if path.exists() else "No hidden commands recorded yet.")


if __name__ == "__main__":
    main(sys.argv[1:])

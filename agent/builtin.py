"""Run a built-in (no model API) command inside the engine and wait for the outcome.

Usage: python -m agent.builtin [--reset] [--record] "Drop the blue box" ["Bring the tallest object" ...]
  --reset   start from a fresh scene (otherwise the scene carries on from the last command)
  --record  record the robot POV and build a Phase 2 clip (runs/builtin-*/clip.mp4)

The built-in runner uses engine-side object positions, so it demonstrates and tests the engine and
the clip pipeline only. Judged runs must use agent.run_agent (Claude driving from camera images).
"""

import sys
import time
import uuid
from datetime import datetime
from pathlib import Path

from agent.run_agent import Engine, finish_clip


def run(command, engine=None, timeout=180, reset=False, record=False, runs_dir="runs", run_dir=None, clip_text=None, on_state=None):
    """run_dir: record into this existing run folder. clip_text: the words shown on the clip (default: command).
    on_state: called with the robot state on every poll, so a caller can log the pickup and the drop as they happen."""
    engine = engine or Engine()
    if reset:
        engine.call("/reset", {})
    body = {"command": command}
    if record:
        run_dir = Path(run_dir) if run_dir else Path(runs_dir) / ("builtin-" + datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6])
        run_dir.mkdir(parents=True, exist_ok=True)
        body["record_dir"] = str((run_dir / "clip_frames").resolve())
    engine.call("/run_builtin", body)
    end = time.time() + timeout
    state = {"state": "?"}
    while time.time() < end:
        time.sleep(0.5)
        state = engine.call("/state")
        if on_state:
            on_state(state)
        if state["state"] == "Done":
            result = {"command": command, "outcome": state["outcome"]}
            if record:
                result["clip"] = finish_clip(engine, run_dir, clip_text or command)
                try:
                    from agent.replay import write_replay
                    write_replay(run_dir)   # keep the replays site up to date
                except Exception:
                    pass
            return result
    return {"command": command, "outcome": "timeout", "state": state["state"]}


if __name__ == "__main__":
    # Scene is kept between commands (delivered objects stay in the zone); pass --reset to start fresh.
    flags = {a for a in sys.argv[1:] if a.startswith("--")}
    commands = [a for a in sys.argv[1:] if not a.startswith("--")]
    for i, command in enumerate(commands):
        print(run(command, reset="--reset" in flags and i == 0, record="--record" in flags))

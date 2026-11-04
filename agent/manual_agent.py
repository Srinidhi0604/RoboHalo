"""Temporary fixed-path Round 1 runner. It uses no cloud/model API."""

import argparse
import json
import math
import uuid
from datetime import datetime, timezone
from pathlib import Path

from agent.run_agent import Engine

# shortcut: known coordinates test plumbing only; remove this runner before judged image-driven runs.
TARGETS = {
    "red sphere": (1.6, -1.6),
    "red cube": (-1.6, -1.6),
    "blue sphere": (2.0, 0.0),
    "blue cube": (-2.0, 0.0),
    "yellow cylinder": (-1.6, 1.6),
    "yellow cone": (0.0, 2.0),
}
ZONE = (2.0, 2.0)

# stop_short: stop this many metres from the object centre before picking up.
# Must be in range (robot_capsule_r + object_r + lookahead, pickup_reach)
# = (0.22 + 0.15 + ~0.03, 0.5) ≈ (0.40, 0.50). Use 0.44 as the midpoint.
STOP_SHORT = 0.44


def resolve_target(command):
    text = command.lower()
    if "tallest" in text:
        return "yellow cylinder"
    color = next((value for value in ("red", "blue", "yellow") if value in text), None)
    shape = "sphere" if any(value in text for value in ("sphere", "round", "ball")) else next((value for value in ("cube", "cylinder", "cone") if value in text), "cube" if "box" in text else None)
    if color and shape and f"{color} {shape}" in TARGETS:
        return f"{color} {shape}"
    matches = [name for name in TARGETS if (not color or name.startswith(color)) and (not shape or name.endswith(shape))]
    if len(matches) == 1:
        return matches[0]
    raise ValueError("Use a command that uniquely names a Round 1 object")


def normalized_turn(current, desired):
    return (desired - current + 180) % 360 - 180


def move_actions(distance):
    actions = []
    while distance > 0.001:
        step = min(1.0, distance)
        actions.append(("move", {"distance": round(step, 3)}))
        distance -= step
    return actions


def leg(start, end, heading, stop_short=0.0):
    dx, dy = end[0] - start[0], end[1] - start[1]
    length = math.hypot(dx, dy)
    desired = math.degrees(math.atan2(-dy, dx)) % 360
    distance = max(0.0, length - stop_short)
    turn = normalized_turn(heading, desired)
    actions = [] if abs(turn) < 0.01 else [("turn", {"degrees": round(turn, 3)})]
    actions.extend(move_actions(distance))
    ratio = 0 if length == 0 else distance / length
    reached = (start[0] + dx * ratio, start[1] + dy * ratio)
    return actions, reached, desired


def build_plan(command):
    target = TARGETS[resolve_target(command)]
    # Leg 1: origin → pickup point (stop STOP_SHORT metres from object centre)
    first, pickup_point, heading = leg((0.0, 0.0), target, 0.0, STOP_SHORT)
    # Leg 2: pickup point → origin
    back, origin, heading = leg(pickup_point, (0.0, 0.0), heading)
    # Leg 3: origin → drop zone
    deliver, _, _ = leg(origin, ZONE, heading)
    return first + [("pickup", {})] + back + deliver + [("drop", {})]


def run(command, engine=None, runs_dir="runs"):
    engine = engine or Engine()
    plan = build_plan(command)
    run_dir = Path(runs_dir) / ("manual-" + datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6])
    run_dir.mkdir(parents=True)
    log_path = run_dir / "run.jsonl"
    engine.call("/reset", {})  # idempotent: resets Done→Idle so consecutive runs work
    engine.call("/start", {"command": command})
    frame_path = None
    previous_tool = None
    with log_path.open("w", encoding="utf-8") as log:
        for step, (tool, args) in enumerate(plan, 1):
            if frame_path is None or previous_tool in ("move", "turn"):
                frame = engine.call("/look")
                frame_path = run_dir / f"frame-{step:03}.jpg"
                frame_path.write_bytes(frame)
            result = engine.call("/" + tool, args)
            record = {"step": step, "timestamp": datetime.now(timezone.utc).isoformat(), "tool": tool, "args": args, "result": result, "frame_path": frame_path.name}
            log.write(json.dumps(record, separators=(",", ":")) + "\n")
            if result.get("collided"):
                raise RuntimeError(f"Manual test path collided at step {step}")
            previous_tool = tool
        state = engine.call("/state")
        summary = {"command": command, "mode": "manual_fixed_path", "outcome": state.get("outcome"), "step_count": len(plan), "retries": state.get("retries", state.get("retry_count", 0))}
        log.write(json.dumps({"summary": summary}, separators=(",", ":")) + "\n")
    return summary


def main():
    parser = argparse.ArgumentParser(description="Run hardcoded Round 1 movements without a model API")
    parser.add_argument("command")
    args = parser.parse_args()
    print(json.dumps(run(args.command), indent=2))


if __name__ == "__main__":
    main()

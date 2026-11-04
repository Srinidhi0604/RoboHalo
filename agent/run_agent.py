"""Phase 1 Claude vision/tool loop for the local Unity engine (+ Phase 2 clip recording).

Usage:
  python -m agent.run_agent "Bring the red cube to the green square."
  python -m agent.run_agent "..." --model claude-haiku-5-5     # cheap plumbing tests
  python -m agent.run_agent "..." --no-record                  # skip the POV clip

Budget rules from the brief, enforced here:
  - one Claude call per step, one tool call out, at most one image in
  - a new image only after /move or /turn (never after /pickup or /drop)
  - only the latest frame is sent; earlier steps go in as one line of text each
  - system prompt + tools first, with prompt caching
  - hard stop at 60 steps; run counter warns at 120 runs
"""

import argparse
import base64
import io
import json
import os
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

DEFAULT_MODEL = "claude-sonnet-5-5"
MAX_STEPS = 60
SOFT_STEPS = 25
RUN_BUDGET_WARNING = 120

# Per million tokens: (input, cache write, cache read, output). Cache write = 1.25x, read = 0.1x input.
PRICES = {
    "claude-sonnet-5-5": (2.00, 2.50, 0.20, 10.00),
    "claude-haiku-5-5": (0.10, 0.125, 0.01, 0.50),
    "claude-opus-5-5": (4.00, 5.00, 0.20, 20.00),
}
# Server-side refusal fallback (Claude API only); Haiku has no server-side fallback.
FALLBACK_MODELS = {"claude-sonnet-5-5", "claude-opus-5-5"}

TOOLS = [
    {"name": "move", "description": "Drive straight. distance is in metres, -1 to 1; negative drives backward. Speed 1 m/s. Stops early if the robot body or gripper touches something; the result then says collided=true and the robot has backed off 0.1 m.",
     "input_schema": {"type": "object", "properties": {"distance": {"type": "number", "minimum": -1, "maximum": 1}}, "required": ["distance"]}},
    {"name": "turn", "description": "Turn in place. degrees is -180 to 180; positive turns right (clockwise seen from above), negative turns left. Speed 90 deg/s. Stops early with collided=true if the gripper or a held object would swing into something.",
     "input_schema": {"type": "object", "properties": {"degrees": {"type": "number", "minimum": -180, "maximum": 180}}, "required": ["degrees"]}},
    {"name": "pickup", "description": "Close the gripper on the object directly in front of it. Works when the object is roughly centred in the image and close: within about 0.5 m of the front of the robot and within 30 degrees of straight ahead. Returns ok=true or ok=false only; it never says what was grabbed.",
     "input_schema": {"type": "object", "properties": {}}},
    {"name": "drop", "description": "Open the gripper and set the held object down about 0.3 m in front of the robot. Returns ok and in_zone (whether it landed in the drop zone). Ends the run.",
     "input_schema": {"type": "object", "properties": {}}},
    {"name": "fail", "description": "Give up and end the run when the task cannot be completed.",
     "input_schema": {"type": "object", "properties": {"reason": {"type": "string"}}, "required": ["reason"]}},
]

SYSTEM = """You drive a small wheeled robot with a two-finger gripper on its front. A user gave it one plain-language fetch command: find the requested object, pick it up, carry it to the marked drop zone, and release it there.

What you can sense:
- One forward camera image (640x480, 90 degree horizontal field of view), mounted 0.5 m above the floor at the front of the robot and tilted 10 degrees down. The lower edge of the image shows the gripper fingers and anything being held. This image is your only source of information about the world. You get a new image only after a move or turn.
- depth_m: distance in metres to the nearest thing straight ahead at camera height, or none if nothing is within 10 m. It does not say what the thing is.
- Robot state: whether the gripper is holding something and how many collisions have happened (3 collisions end the run as a failure).

How to work, one tool call per step:
1. Read the command carefully. Commands can need reasoning, for example "the round one" or "the tallest object". Decide which object fits by comparing what you see.
2. To search, turn about 45 degrees and look again. Eight turns cover a full circle. If nothing fits after a full circle, drive about 1-2 m toward open floor and search again.
3. Steer so the target is in the middle of the image. An object left of centre needs a negative (left) turn; right of centre needs a positive turn. With a 90 degree field of view, an object at the left or right image edge is about 45 degrees off.
4. Approach in steps: use moves of up to 1 m while far, then 0.2-0.3 m when close. Re-centre with small turns as you go. When the target sits between the gripper fingers at the bottom of the image (depth_m around 0.3 or less), call pickup.
5. After a successful pickup, find the drop zone the same way, drive until it fills the lower part of the image just ahead of the gripper, then call drop.
6. If a move or turn reports collided=true, do not repeat it. Back up (negative move) or turn 30-60 degrees away, then take a different path around the obstacle.
7. Be efficient. Aim to finish in about 25 steps; the run is stopped at 60.

Each step, write one short sentence saying what you see and why you chose your action, then call exactly one tool."""


class Engine:
    def __init__(self, base="http://127.0.0.1:8080"):
        self.base = base.rstrip("/")

    def call(self, path, body=None, timeout=30):
        data = None if body is None else json.dumps(body).encode()
        request = Request(self.base + path, data=data, headers={"Content-Type": "application/json"})
        with urlopen(request, timeout=timeout) as response:
            content = response.read()
            return content if response.headers.get_content_type() == "image/jpeg" else json.loads(content)


NL = "\n"


class RefusalError(RuntimeError):
    pass


def status_text(step, depth, state, has_image):
    lines = [
        f"Step {step} of {MAX_STEPS} (aim to finish within {SOFT_STEPS}).",
        f"Holding: {'yes' if state.get('holding') else 'no'}. Collisions so far: {state.get('retry_count', 0)} of 3.",
        f"depth_m: {'none' if depth is None else round(depth, 2)}",
    ]
    if not has_image:
        lines.append("No new image this step (the last action was pickup or drop, or failed); the scene ahead is unchanged.")
    return NL.join(lines)


def image_block(frame):
    return {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": base64.b64encode(frame).decode()}}


def build_messages(command, frame, depth, state, history, step, previous):
    """Command + one line per earlier step, then the previous tool call and its result.

    The latest /look image travels inside the tool_result of the move/turn that produced it,
    as the brief asks. Only that one image is ever sent.
    """
    context = [f"Command: {command}"]
    earlier = history[:-1] if previous else history
    if earlier:
        context += ["Earlier steps:"] + earlier[-12:]
    status = status_text(step, depth, state, frame is not None)
    if not previous:
        content = ([image_block(frame)] if frame is not None else []) + [{"type": "text", "text": NL.join(context) + NL + status}]
        return [{"role": "user", "content": content}]
    result = [{"type": "text", "text": json.dumps(previous["result"], separators=(",", ":")) + NL + status}]
    if frame is not None:
        result.insert(0, image_block(frame))
    return [
        {"role": "user", "content": [{"type": "text", "text": NL.join(context)}]},
        {"role": "assistant", "content": [{"type": "tool_use", "id": previous["id"], "name": previous["name"], "input": previous["input"]}]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": previous["id"], "content": result}]},
    ]


def request_kwargs(model, messages):
    kwargs = {
        "model": model,
        "max_tokens": 1024,  # thinking counts toward max_tokens
        "system": [{"type": "text", "text": SYSTEM, "cache_control": {"type": "ephemeral"}}],  # tools + system cached
        "tools": TOOLS,
        "tool_choice": {"type": "auto", "disable_parallel_tool_use": True},  # forced tool_choice is a 400 on Sonnet 5.5
        "messages": messages,
        "extra_body": {"output_config": {"effort": "low"}},  # short per-step decisions; keeps thinking tokens low
    }
    if model in FALLBACK_MODELS:
        kwargs["betas"] = ["server-side-fallback-2026-07-01"]
        kwargs["extra_body"]["fallbacks"] = "default"
    return kwargs


def choose_tool(client, model, command, frame, depth, state, history, step=1, previous=None):
    """One Claude call. Returns (tool name or None, args, usage, reasoning text, tool_use id)."""
    messages = build_messages(command, frame, depth, state, history, step, previous)
    response = client.beta.messages.create(**request_kwargs(model, messages))
    if getattr(response, "stop_reason", None) == "refusal":
        raise RefusalError(str(getattr(response, "stop_details", "") or "refused"))
    calls = [b for b in response.content if getattr(b, "type", None) == "tool_use"]
    reasoning = " ".join(b.text.strip() for b in response.content if getattr(b, "type", None) == "text" and b.text.strip())
    usage = getattr(response, "usage", None)
    if not calls:
        return None, {}, usage, reasoning, None
    return calls[0].name, dict(calls[0].input), usage, reasoning, calls[0].id


def validate(name, args):
    if name not in {tool["name"] for tool in TOOLS} or not isinstance(args, dict):
        raise ValueError("Unknown or malformed tool call")
    if name == "move" and not -1 <= float(args.get("distance", 99)) <= 1:
        raise ValueError("Move must be between -1 and 1 metre")
    if name == "turn" and not -180 <= float(args.get("degrees", 999)) <= 180:
        raise ValueError("Turn must be between -180 and 180 degrees")


def step_cost(model, usage):
    if usage is None:
        return 0.0
    price = PRICES.get(model, PRICES[DEFAULT_MODEL])
    tokens = [getattr(usage, k, 0) or 0 for k in ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens", "output_tokens")]
    return sum(t * p for t, p in zip(tokens, price)) / 1_000_000


def history_line(step, name, args, result):
    arg = ", ".join(f"{k}={v}" for k, v in args.items())
    keep = {k: result[k] for k in ("ok", "collided", "moved", "heading", "in_zone", "error") if k in result}
    return f"{step}. {name or 'no tool'}({arg}) -> {json.dumps(keep, separators=(',', ':'))}"


def save_png(jpeg_bytes, path):
    from PIL import Image
    Image.open(io.BytesIO(jpeg_bytes)).save(path, "PNG")


def count_run(runs_dir):
    counter = Path(runs_dir) / "run_count.json"
    count = json.loads(counter.read_text())["claude_runs"] + 1 if counter.exists() else 1
    counter.write_text(json.dumps({"claude_runs": count}))
    if count >= RUN_BUDGET_WARNING:
        print(f"WARNING: {count} Claude runs used. The brief says stop developing at {RUN_BUDGET_WARNING} and keep the rest for rehearsal and hidden commands.")
    return count


def call_engine(engine, path, body=None):
    try:
        return engine.call(path, body)
    except HTTPError as error:
        try:
            detail = json.loads(error.read()).get("error", "")
        except Exception:
            detail = ""
        return {"ok": False, "error": f"HTTP {error.code} {detail}".strip()}


def finish_clip(engine, run_dir, command):
    """Wait for the engine to finish its final hold, then assemble the mp4."""
    from agent.clip import build_clip
    for _ in range(40):
        status = call_engine(engine, "/record")
        if not status.get("recording", False):
            break
        time.sleep(0.25)
    frames = run_dir / "clip_frames"
    if not any(frames.glob("frame_*.jpg")):
        return {"ok": False, "problems": ["no frames recorded"]}
    return build_clip(frames, command, run_dir / "clip.mp4")


def run(command, client, model, engine=None, runs_dir="runs", max_steps=MAX_STEPS, record=False):
    engine = engine or Engine()
    run_dir = Path(runs_dir) / (datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6])
    run_dir.mkdir(parents=True)
    run_number = count_run(runs_dir)
    log_path = run_dir / "run.jsonl"
    history, cost = [], 0.0
    tokens = {"input": 0, "cache_write": 0, "cache_read": 0, "output": 0}
    start_body = {"command": command}
    if record:
        start_body["record_dir"] = str((run_dir / "clip_frames").resolve())
    engine.call("/start", start_body)
    need_look, frame, frame_name = True, None, None
    outcome, step, previous = None, 0, None

    with log_path.open("w", encoding="utf-8") as log:
        for step in range(1, max_steps + 1):
            sent = None
            if need_look:
                frame = engine.call("/look")
                frame_name = f"frame-{step:03}.png"
                save_png(frame, run_dir / frame_name)
                sent = frame
            state = engine.call("/state")
            depth = engine.call("/depth")
            name, args, reasoning, usage, call_id = None, {}, "", None, None
            try:
                name, args, usage, reasoning, call_id = choose_tool(client, model, command, sent, depth, state, history, step, previous)
            except RefusalError as error:
                result, outcome = {"ok": False, "error": f"model refused: {error}"}, "fail_not_reached"
            except Exception as error:  # API failure after the SDK's own retries
                result, outcome = {"ok": False, "error": f"API error: {error}"}, "fail_not_reached"
            else:
                if name is None:
                    result = {"ok": False, "error": "no tool call; call exactly one tool"}
                elif name == "fail":
                    result, outcome = {"ok": False, "reason": args.get("reason", "")}, "fail_not_reached"
                else:
                    try:
                        validate(name, args)
                        result = call_engine(engine, "/" + name, args if name in ("move", "turn") else {})
                    except ValueError as error:
                        result = {"ok": False, "error": str(error)}
                    after = engine.call("/state")
                    if after.get("state") == "Done":
                        outcome = after.get("outcome") or "fail_not_reached"
            if usage is not None:
                for key, field in (("input", "input_tokens"), ("cache_write", "cache_creation_input_tokens"), ("cache_read", "cache_read_input_tokens"), ("output", "output_tokens")):
                    tokens[key] += getattr(usage, field, 0) or 0
            spent = step_cost(model, usage)
            cost += spent
            record_line = {"step": step, "timestamp": datetime.now(timezone.utc).isoformat(), "tool": name, "args": args, "result": result,
                           "frame_path": frame_name if sent is not None else None, "reasoning": reasoning, "depth_m": depth,
                           "state": {"holding": state.get("holding"), "retries": state.get("retry_count")}, "cost_usd": round(spent, 5)}
            log.write(json.dumps(record_line, separators=(",", ":")) + "\n")
            log.flush()
            history.append(history_line(step, name, args, result))
            previous = {"id": call_id, "name": name, "input": args, "result": result} if call_id else None
            # Look again only after the robot actually moved or turned.
            need_look = name in ("move", "turn") and not result.get("error")
            if outcome:
                break
        if not outcome:
            outcome = "fail_not_reached"  # hard stop at max_steps, no exceptions
        final_state = call_engine(engine, "/state")
        summary = {"command": command, "outcome": outcome, "step_count": step, "retries": final_state.get("retry_count"),
                   "model": model, "tokens": tokens, "cost_estimate_usd": round(cost, 6), "run_number": run_number}
        if record:
            try:
                summary["clip"] = finish_clip(engine, run_dir, command)
            except Exception as error:
                summary["clip"] = {"ok": False, "problems": [str(error)]}
        log.write(json.dumps({"summary": summary}, separators=(",", ":")) + "\n")
    return summary


def main():
    parser = argparse.ArgumentParser(description="Claude drives the robot from camera images.")
    parser.add_argument("command")
    parser.add_argument("--model", default=os.getenv("CLAUDE_MODEL", DEFAULT_MODEL))
    parser.add_argument("--no-record", action="store_true", help="do not record the POV clip")
    args = parser.parse_args()
    from anthropic import Anthropic
    print(json.dumps(run(args.command, Anthropic(), args.model, record=not args.no_record), indent=2))


if __name__ == "__main__":
    main()

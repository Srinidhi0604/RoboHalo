"""The Speak button: voice -> text -> Claude decides -> the engine acts.

1. Unity records the microphone; `agent.transcribe` turns the recording into text. Voice and
   transcription are used for nothing else.
2. Claude is the decision maker only. It gets the transcribed request and one sheet of 8 camera
   views (a full circle), and answers with the colour and shape of the single item to fetch.
   It is told nothing about the scene: colour and shape come from the pixels.
3. The decision is sent back to the Unity panel, and the engine's own scripted routine does the
   driving ("Bring the <colour> <shape>"). Claude does not steer in this mode.

Because the engine's routine drives with its own knowledge of the scene, this is an operator/demo
mode. Judged Phase 1 runs use the typed command path in `agent.run_agent`, where Claude drives from
camera images alone.

  python -m agent.speak "bring me the round red thing"     # test the decision + action without a microphone
"""

import json
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError

from agent.builtin import run as run_builtin
from agent.run_agent import Engine, call_engine, count_run, image_block, make_client, save_png, step_cost

# General vocabulary, not a list of what is in the room.
COLOURS = ["red", "blue", "yellow", "green", "orange", "purple", "pink", "brown", "black", "white", "grey"]
SHAPES = ["cube", "sphere", "cylinder", "cone"]

SYSTEM = """You choose which single item a fetch robot should bring. You do not drive it.

The image is a sheet of 8 numbered camera views from the robot, taken by turning through a full circle with 45 degrees between views, so neighbouring views overlap. The request was typed or spoken; a spoken one is transcribed, so a word may be misheard (for example "read" for "red", "spear" for "sphere"): read it generously.

Look at every view. Compare the loose items standing by themselves on the floor by colour, shape and size, and choose the one that best fits the request, including indirect requests such as "the tallest one" or "something that rolls". Check the colour and the shape separately and report them exactly as you see them: a box is a cube, a ball is a sphere, a can or pillar is a cylinder, a pointed item is a cone. Ignore fixtures and stored goods. If nothing in the views fits, set found to false. Never guess about things that are not in the image.

Call decide exactly once."""

DECIDE = {"name": "decide", "description": "The one item to fetch, as seen in the camera views.",
          "input_schema": {"type": "object", "properties": {
              "found": {"type": "boolean"},
              "colour": {"type": "string", "enum": COLOURS},
              "shape": {"type": "string", "enum": SHAPES},
              "view": {"type": "integer", "minimum": 1, "maximum": 8, "description": "the numbered view that shows it best"},
              "reason": {"type": "string", "description": "one short sentence: what you saw and why it fits"}},
              "required": ["found", "reason"]}}


def look_around(engine):
    """8 camera frames, 45 degrees apart; the robot ends facing the way it started."""
    if call_engine(engine, "/state").get("state") not in ("Idle", None):
        engine.call("/reset", {})  # a finished or abandoned run blocks turning
    frames = []
    for _ in range(8):
        frames.append(engine.call("/look"))
        call_engine(engine, "/turn", {"degrees": 45})
    return frames


def decide(request, sheet, client, model):
    """One Claude call. Returns ({'found', 'colour', 'shape', 'view', 'reason'}, usage)."""
    answer, response = _decide(request, sheet, client, model)
    return answer, getattr(response, "usage", None)


def _decide(request, sheet, client, model):
    response = client.beta.messages.create(
        model=model, max_tokens=1024, system=[{"type": "text", "text": SYSTEM}], tools=[DECIDE], tool_choice={"type": "auto"},
        messages=[{"role": "user", "content": [image_block(sheet), {"type": "text", "text": f'Request: "{request}"'}]}])
    for block in response.content:
        if getattr(block, "type", None) == "tool_use" and block.name == "decide":
            answer = dict(block.input)
            ok = bool(answer.get("found")) and answer.get("colour") in COLOURS and answer.get("shape") in SHAPES
            return {**answer, "found": ok, "reason": str(answer.get("reason", "")).strip()}, response
    text = " ".join(b.text for b in response.content if getattr(b, "type", None) == "text").strip()
    return {"found": False, "reason": text or "Claude gave no decision."}, response


def handle(request, client, model, engine, tell=print, runs_dir="runs"):
    """Request -> Claude's decision (sent back through `tell`) -> scripted fetch. Returns the run summary.

    The run folder holds the same files as a camera-agent run: run.jsonl (one line per step, then the
    summary), the camera frames as PNG, clip_frames + clip.mp4 + clip.json, and replay.html.
    """
    from agent.scout import make_sheet
    tell(f'request "{request}". Looking around...')
    folder = Path(runs_dir) / ("speak-" + datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6])
    folder.mkdir(parents=True)
    steps = []

    def log(tool, args, result, frame=None, reasoning="", cost=0.0):
        steps.append({"step": len(steps) + 1, "timestamp": datetime.now(timezone.utc).isoformat(), "tool": tool, "args": args,
                      "result": result, "frame_path": frame, "reasoning": reasoning, "cost_usd": round(cost, 5)})

    frames = look_around(engine)
    for i, frame in enumerate(frames):
        save_png(frame, folder / f"frame-{i + 1:03}.png")
        log("look", {"heading": i * 45}, {"ok": True}, f"frame-{i + 1:03}.png")
    sheet = make_sheet(frames)
    sheet_name = f"frame-{len(frames) + 1:03}.png"   # the one image Claude actually saw
    save_png(sheet, folder / sheet_name)
    choice, usage = decide(request, sheet, client, model)
    cost = step_cost(model, usage)
    log("decide", {"request": request}, {k: v for k, v in choice.items() if k != "reason"}, sheet_name, choice["reason"], cost)

    summary = {"command": request, "outcome": "fail_not_reached", "model": model, "mode": "decide_then_manual", "decision": choice,
               "cost_estimate_usd": round(cost, 6), "run_number": count_run(runs_dir) if str(model).startswith("claude") else 0,
               "run_dir": str(folder)}
    if not choice["found"]:
        summary["problem"] = "Claude found nothing that fits. " + choice["reason"]
        tell("Claude decided: nothing here fits. " + choice["reason"])
    else:
        target = f"{choice['colour']} {choice['shape']}"
        tell(f"Claude decided: {target}. {choice['reason']}")
        picked = []

        def frame_now():
            name = f"frame-{len(steps) + 1:03}.png"
            save_png(engine.call("/look"), folder / name)
            return name

        def watch(state):  # the manual run: log the pickup the moment the robot is holding the item
            if state.get("holding") and not picked:
                picked.append(True)
                log("pickup", {"target": target}, {"ok": True}, frame_now(), f"Manual pick: drove to the {target} and picked it up.")

        def manual_run():
            return run_builtin("Bring the " + target, engine, record=True, run_dir=folder, clip_text=request, on_state=watch)

        try:
            try:
                done = manual_run()
            except HTTPError as error:
                if error.code != 409:
                    raise
                engine.call("/reset", {})  # something else was still running on the robot: clear it and try once more
                done = manual_run()
            outcome = done["outcome"]
            summary["outcome"] = outcome
            summary["clip"] = done.get("clip")
            if not picked:
                log("pickup", {"target": target}, {"ok": False}, None, f"Manual pick: the {target} was not picked up.")
            log("drop", {}, {"ok": outcome == "pass", "in_zone": outcome in ("pass", "fail_wrong_object"), "outcome": outcome}, frame_now(),
                "Manual drop: carried it to the drop zone and released it." if outcome == "pass" else f"Manual drop did not finish: {outcome.replace('_', ' ')}.")
            tell(f"Claude decided: {target}. Result: {outcome.replace('_', ' ')}.")
        except Exception as error:  # the engine has no such item, or it was already delivered
            summary["problem"] = f"The robot could not act on '{target}': {error}"
            log("pickup", {"target": target}, {"ok": False, "error": str(error)}, None, "Manual pick could not start.")
            tell(f"Claude decided: {target}, but the robot could not act on it ({error}).")
    summary["step_count"] = len(steps)
    with (folder / "run.jsonl").open("w", encoding="utf-8") as out:
        for line in steps + [{"summary": summary}]:
            out.write(json.dumps(line, separators=(",", ":")) + "\n")
    try:
        from agent.replay import write_replay
        write_replay(folder)
    except Exception as error:
        print("replay page not written:", error)
    return summary


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    client, model, _ = make_client()
    print(json.dumps(handle(sys.argv[1], client, model, Engine()), indent=2))

"""Quick mode: one model call turns a spoken or typed request into basic commands the engine runs directly.

  python -m agent.quick "bring me something to drink"     # typed
  python -m agent.quick                                    # spoken (confirm what was heard)
  python -m agent.quick --yes --loop                       # hands-free, keeps taking requests

The model gets a short description of the app and its objects, and answers with a list of basic
commands ("fetch yellow cylinder", "dark mode on", ...). The engine's own planner then does the
driving, so a request finishes in seconds instead of minutes.

This mode tells the model what is in the scene, so it is a demo and testing mode. Judged Phase 1
runs must use `agent.run_agent`, where the model sees only camera images.
"""

import argparse
import json
import random
import re
import sys
import uuid
from datetime import datetime
from pathlib import Path
from urllib.error import HTTPError

from agent.builtin import run as run_builtin
from agent.run_agent import Engine, make_client

OBJECTS = ["red sphere", "red cube", "blue sphere", "blue cube", "yellow cylinder", "yellow cone"]
VIEWS = {"camera overview": 0, "camera follow": 1, "camera top-down": 2, "camera robot": 3}
SWITCHES = {"dark mode on": ("/dark", True), "dark mode off": ("/dark", False),
            "detour wall on": ("/obstacle", True), "detour wall off": ("/obstacle", False)}
COMMANDS = ["fetch " + name for name in OBJECTS] + ["reset scene", "shuffle layout"] + list(SWITCHES) + list(VIEWS)

APP_CONTEXT = """You are the command interpreter for RoboHalo, a warehouse robot simulator.

The app: a small wheeled robot with a front gripper works in a warehouse room. On a request it drives to an object, picks it up, carries it to the green drop zone and sets it down. An operator talks to it in plain language, by voice or text. Speech recognition can mishear words (for example "read cube" means "red cube", "blue spear" means "blue sphere"), so interpret generously.

Objects on the floor (each can be fetched once until the scene is reset):
- red sphere: a red ball, round
- red cube: a red box
- blue sphere: a blue ball, round
- blue cube: a blue box
- yellow cylinder: the tallest object, shaped like a can or bottle (the best match for "something to drink")
- yellow cone: pointed, like a traffic cone or party hat

Basic commands you can issue:
- "fetch <object>": bring that object to the drop zone
- "reset scene": put every object and the robot back
- "shuffle layout": reset with the objects in new random places
- "dark mode on" / "dark mode off": room lights off (robot uses its infrared camera) or on
- "detour wall on" / "detour wall off": add or remove a wall between the robot and the drop zone
- "camera overview" / "camera follow" / "camera top-down" / "camera robot": change the view on screen

Turn the operator's request into the shortest list of basic commands that does what they asked, in the order they should run. A request can need several commands ("bring all the red things" is two fetches; "everything" is all six). Pick the best match when the request is indirect ("the round red thing", "the tallest one"). If the request cannot be done with these commands, return an empty list and say why in one short sentence.

Call the plan tool exactly once."""

PLAN_TOOL = {"name": "plan", "description": "The basic commands to run, in order, and a one-sentence note for the operator.",
             "input_schema": {"type": "object", "properties": {
                 "steps": {"type": "array", "items": {"type": "string", "enum": COMMANDS}},
                 "note": {"type": "string"}}, "required": ["steps", "note"]}}


def clean_steps(steps):
    """Keep only known commands, without repeating a fetch."""
    seen, out = set(), []
    for step in steps if isinstance(steps, list) else []:
        step = str(step).strip().lower()
        if step in COMMANDS and not (step.startswith("fetch ") and step in seen):
            out.append(step)
            seen.add(step)
    return out


def interpret(request, client, model):
    """One model call. Returns (steps, note)."""
    response = client.beta.messages.create(
        model=model, max_tokens=400, system=[{"type": "text", "text": APP_CONTEXT}], tools=[PLAN_TOOL],
        tool_choice={"type": "auto"}, messages=[{"role": "user", "content": [{"type": "text", "text": "Operator request: " + request}]}])
    text = " ".join(b.text for b in response.content if getattr(b, "type", None) == "text")
    for block in response.content:
        if getattr(block, "type", None) == "tool_use" and block.name == "plan":
            return clean_steps(block.input.get("steps")), str(block.input.get("note") or text).strip()
    # No tool call: fall back to any basic commands written in the reply text.
    found = [c for c in COMMANDS if re.search(r"\b" + re.escape(c) + r"\b", text.lower())]
    return clean_steps(sorted(found, key=lambda c: text.lower().index(c))), text.strip()


def execute(steps, engine, record=False, say=print):
    """Run basic commands on the engine. Returns one result line per step."""
    results = []
    for step in steps:
        try:
            if step.startswith("fetch "):
                outcome = run_builtin("Bring the " + step[6:], engine, record=record)
                result = outcome["outcome"]
                if record and outcome.get("clip"):
                    result += " (clip: " + str(outcome["clip"].get("path")) + ")"
            elif step == "reset scene":
                engine.call("/reset", {})
                result = "done"
            elif step == "shuffle layout":
                seed = random.randint(1, 99999)
                engine.call("/reset", {"seed": seed})
                result = f"done (seed {seed})"
            elif step in SWITCHES:
                path, on = SWITCHES[step]
                engine.call(path, {"on": on})
                result = "done"
            else:
                engine.call("/view", {"index": VIEWS[step]})
                result = "done"
        except HTTPError as error:
            detail = ""
            try:
                detail = json.loads(error.read()).get("error", "")
            except Exception:
                pass
            result = f"not run: {detail or 'HTTP ' + str(error.code)}"
        say(f"  {step}: {result}")
        results.append({"step": step, "result": result})
    return results


def handle(request, client, model, engine, record=False, runs_dir="runs", say=print):
    steps, note = interpret(request, client, model)
    say(f'Request: "{request}"')
    say("Plan: " + (", ".join(steps) if steps else "nothing to run") + (f"  ({note})" if note else ""))
    results = execute(steps, engine, record, say)
    folder = Path(runs_dir) / ("quick-" + datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6])
    folder.mkdir(parents=True)
    record_line = {"request": request, "model": model, "steps": steps, "note": note, "results": results}
    (folder / "plan.json").write_text(json.dumps(record_line, indent=2), encoding="utf-8")
    return record_line


def main(argv=None):
    parser = argparse.ArgumentParser(description="Say or type a request; one model call plans it and the engine runs it.")
    parser.add_argument("request", nargs="?", help="typed request; leave out to speak it")
    parser.add_argument("--yes", action="store_true", help="voice: run without asking for confirmation")
    parser.add_argument("--loop", action="store_true", help="voice: keep taking requests until Ctrl+C")
    parser.add_argument("--record", action="store_true", help="record a POV clip for each fetch")
    parser.add_argument("--provider", choices=["claude", "gemini", "groq"])
    parser.add_argument("--model")
    args = parser.parse_args(argv)
    client, model, _ = make_client(args.provider, args.model)
    print("Quick mode: the model is told what is in the scene, so use this for demos and tests, not judged runs.")
    engine = Engine()
    while True:
        request = args.request
        if not request:
            from agent.voice import hear_command
            request = hear_command(auto_yes=args.yes)
        if not request:
            print("No request heard.")
            return 1
        handle(request, client, model, engine, args.record)
        if args.request or not args.loop:
            return 0


if __name__ == "__main__":
    sys.exit(main())

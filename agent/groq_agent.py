"""Drive the Unity TurtleBot from camera images with Groq vision tool calls.

Run: python -m agent.groq_agent "Bring the round red thing"
Or start the Unity command-field bridge: python -m agent.groq_agent --listen
"""

import argparse
import base64
import json
import traceback
import os
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from agent.run_agent import Engine, GO_TO_HINT, SYSTEM, TOOLS as ROBOT_TOOLS, call_engine, history_line, load_env, save_png, validate

DEFAULT_MODEL = "qwen/qwen3.8-27b"
API_URL = "https://api.groq.com/openai/v1/chat/completions"
MAX_STEPS = 60
TOOLS = [{"type": "function", "function": {"name": tool["name"], "description": tool["description"], "parameters": tool["input_schema"]}}
         for tool in ROBOT_TOOLS if tool["name"] != "go_to"]
ALLOWED = {tool["function"]["name"] for tool in TOOLS}


def choose_action(key, model, command, frame, depth, state, history):
    """One model request, one image, and exactly one accepted tool call."""
    status = (f"Command: {command}\nRobot position: x={state.get('x', 0):.2f}, "
              f"y={state.get('y', 0):.2f}, heading={state.get('heading', 0):.0f} degrees. "
              f"Holding: {bool(state.get('holding'))}. Retries: {state.get('retries', 0)}. "
              f"Depth metres: {depth}.\nRecent actions:\n" + "\n".join(history[-8:]) +
              "\nInspect the image and call exactly one robot action tool.")
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": SYSTEM.replace(GO_TO_HINT, "")},
            {"role": "user", "content": [
                {"type": "text", "text": status},
                {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + base64.b64encode(frame).decode("ascii")}},
            ]},
        ],
        "tools": TOOLS,
        "tool_choice": "required",
        "parallel_tool_calls": False,
        "max_completion_tokens": 512,
    }
    request = Request(API_URL, data=json.dumps(payload).encode("utf-8"),
                      headers={"Authorization": "Bearer " + key, "Content-Type": "application/json"})
    try:
        with urlopen(request, timeout=90) as response:
            answer = json.load(response)
    except HTTPError as error:
        detail = error.read().decode("utf-8", errors="replace")[:500]
        raise RuntimeError(f"Groq HTTP {error.code}: {detail}") from error
    calls = answer["choices"][0]["message"].get("tool_calls") or []
    if len(calls) != 1:
        raise RuntimeError(f"Expected one Groq tool call; received {len(calls)}")
    name = calls[0]["function"]["name"]
    if name not in ALLOWED:
        raise RuntimeError(f"Unsupported tool: {name}")
    args = json.loads(calls[0]["function"]["arguments"])
    validate(name, args)
    return name, args


def run(command, key, model=DEFAULT_MODEL, engine=None, max_steps=MAX_STEPS, record=False):
    engine = engine or Engine()
    run_dir = Path("runs") / (datetime.now().strftime("%Y%m%d-%H%M%S") + "-groq-" + uuid.uuid4().hex[:6])
    run_dir.mkdir(parents=True)
    start = {"command": command}
    if record:
        start["record_dir"] = str((run_dir / "clip_frames").resolve())
    engine.call("/start", start)
    history = []
    outcome = None
    with (run_dir / "run.jsonl").open("w", encoding="utf-8") as log:
        for step in range(1, max_steps + 1):
            frame = engine.call("/look")
            frame_name = f"frame-{step:03d}.png"
            save_png(frame, run_dir / frame_name)
            state = engine.call("/state")
            depth = engine.call("/depth")
            try:
                name, args = choose_action(key, model, command, frame, depth, state, history)
                result = call_engine(engine, "/fail", {}) if name == "fail" else call_engine(engine, "/" + name, args if name in ("move", "turn") else {})
            except (RuntimeError, ValueError, HTTPError, URLError) as error:
                name, args, result = None, {}, {"ok": False, "error": str(error)}
                outcome = "fail_not_reached"
                call_engine(engine, "/fail", {})
            after = engine.call("/state")
            if name == "fail":
                outcome = "fail_not_reached"
            elif after.get("state") == "Done":
                outcome = after.get("outcome") or "fail_not_reached"
            log.write(json.dumps({"step": step, "timestamp": datetime.now(timezone.utc).isoformat(),
                                  "tool": name, "args": args, "result": result, "frame_path": frame_name,
                                  "depth_m": depth, "state": {"holding": state.get("holding"), "retries": state.get("retries")}}) + "\n")
            log.flush()
            history.append(history_line(step, name, args, result))
            print(f"{step}: {name} {args} -> {result}", flush=True)
            if outcome:
                break
        summary = {"command": command, "outcome": outcome or "fail_not_reached", "steps": step, "model": model,
                   "run_dir": str(run_dir)}
        if record:
            from agent.run_agent import finish_clip
            try:
                summary["clip"] = finish_clip(engine, run_dir, command)
            except Exception as error:
                summary["clip"] = {"ok": False, "error": str(error)}
        log.write(json.dumps({"summary": summary}) + "\n")
    return summary


def main():
    parser = argparse.ArgumentParser(description="Use Groq vision to drive the Unity TurtleBot from plain-language commands")
    parser.add_argument("command", nargs="?", help="One command, such as 'Bring the round red thing'")
    parser.add_argument("--listen", action="store_true", help="Wait for commands submitted in the Unity CUSTOM COMMAND field")
    parser.add_argument("--port", type=int, default=8080, help="Unity HTTP port shown in the Game view (default: 8080)")
    parser.add_argument("--model", default=os.getenv("GROQ_MODEL", DEFAULT_MODEL))
    parser.add_argument("--record", action="store_true", help="Also build a POV clip; needs video dependencies")
    args = parser.parse_args()
    if not args.listen and not args.command:
        parser.error("provide a command or --listen")
    load_env()
    key = os.getenv("GROQ_API_KEY")
    if not key:
        parser.error("set GROQ_API_KEY in the shell or E:/ansu-hack/.env")
    engine = Engine(f"http://127.0.0.1:{args.port}")
    if args.listen:
        print(f"Waiting for Unity AI COMMAND > Send at http://127.0.0.1:{args.port}/ ...", flush=True)
        while True:
            try:
                command = engine.call("/agent_command").get("command")
                if command:
                    print(json.dumps(run(command, key, args.model, engine, record=args.record), indent=2), flush=True)
                else:
                    time.sleep(0.5)
            except KeyboardInterrupt:
                break
            except (OSError, TimeoutError) as error:
                print(f"Waiting for Unity: {error}", flush=True)
                time.sleep(2)
            except Exception:
                traceback.print_exc()
                time.sleep(2)
    else:
        print(json.dumps(run(args.command, key, args.model, engine, record=args.record), indent=2))


if __name__ == "__main__":
    main()

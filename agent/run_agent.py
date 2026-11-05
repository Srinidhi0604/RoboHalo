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
    {"name": "go_to", "description": "Drive to a floor point by the shortest collision-free route; the robot plans the path and steers around obstacles itself. x and y are in metres, in the same frame as your Position. stop_short (0 to 2, default 0) stops that many metres before the point: use about 0.7 when the point is something you want to pick up, and about 0.6 when it is where you want to drop. Returns reached, collided and the new position.",
     "input_schema": {"type": "object", "properties": {"x": {"type": "number"}, "y": {"type": "number"}, "stop_short": {"type": "number", "minimum": 0, "maximum": 2}}, "required": ["x", "y"]}},
    {"name": "fail", "description": "Give up and end the run when the task cannot be completed.",
     "input_schema": {"type": "object", "properties": {"reason": {"type": "string"}}, "required": ["reason"]}},
]

GO_TO_HINT = """8. go_to saves steps on long drives. Work out the target point from your Position and what you see: a point d metres straight ahead of you is at x + d*cos(heading), y - d*sin(heading), with heading in degrees. depth_m gives d when the target is centred in the image. Use turn and short moves for the final alignment.
9. depth_m passes over anything flat on the floor, so judge floor distance from the image row instead: a floor point at the bottom edge is about 0.5 m ahead of you, three quarters of the way down about 0.9 m, 60 percent down about 1.6 m, and at mid-height about 2.8 m. To deliver, make one go_to to the middle of the drop zone with stop_short 0.6: do not creep up in several short hops. Its middle is halfway between its near and far edges, which is farther than it looks, so work out both edges from their image rows.
"""

SYSTEM = """You drive a small wheeled robot with a two-finger gripper on its front. A user gave it one plain-language fetch command: find the requested object, pick it up, carry it to the marked drop zone, and release it there.

What you can sense:
- One forward camera image (640x480, 90 degree horizontal field of view), mounted 0.5 m above the floor at the front of the robot and tilted 10 degrees down. The lower edge of the image shows the gripper fingers. A held item is lifted above the camera's view, so you cannot see it while carrying. This image is your only source of information about the world. You get a new image only after a move or turn.
- depth_m: distance in metres to the nearest thing straight ahead at camera height, or none if nothing is within 10 m. It does not say what the thing is.
- In a dark room the camera works in infrared: the image is grey and colours cannot be told apart, but shape and size still show. If the command depends on a colour you cannot see, do not guess: use fail and say why.
- Position: your own x, y in metres and heading in degrees (0 = +x axis, increasing clockwise seen from above). It says nothing about where anything else is.
- Robot state: whether the gripper is holding something and how many collisions have happened (3 collisions end the run as a failure).

How to work, one tool call per step:
1. Read the command carefully. Commands can need reasoning, for example "the round one" or "the tallest object". Decide which object fits by comparing what you see. A command that compares (tallest, biggest, nearest to something) can only be answered once you have seen the whole room: finish the full circle before choosing. Some things in view may be fixtures or stored goods that the robot cannot take. When several things could fit the command, prefer one standing by itself on open floor, and treat a pickup that fails twice at close range as proof that thing is not the target.
2. To search, turn exactly 45 degrees, always in the same direction, and look again. Eight looks cover a full circle; the status lists the headings you have already viewed from this spot, so never view one twice. If something in view clearly fits the command, go to it at once. Otherwise finish the circle before driving anywhere, noting each possible candidate and its heading in your sentence; then check the candidates, nearest first. Never drive off toward something you cannot identify while part of the circle is still unseen. If nothing fits after a full circle, drive about 1-2 m toward open floor and search again. If nothing fits after full circles from three spots, call fail and say what you saw instead.
3. Steer so the target is in the middle of the image. An object left of centre needs a negative (left) turn; right of centre needs a positive turn. With a 90 degree field of view, an object at the left or right image edge is about 45 degrees off, and one halfway between the centre and an edge about 25. Turn by the whole offset in one go. A target inside the middle third of the image is centred enough: do not turn, drive, and correct only if it leaves the middle third. Never follow a turn with a turn back the other way.
4. Approach in steps: use moves of up to 1 m while far, then 0.2-0.3 m when close. Re-centre with small turns as you go. When the target sits between the gripper fingers at the bottom of the image (depth_m 0.25 or less), call pickup. If pickup returns ok=false you are not close enough or not centred: never call pickup twice from the same pose. Move 0.1 m forward (the open fingers pass either side of the object) or re-centre, then try again.
5. Whenever the drop zone is in view, at any point in the run, say so in your sentence with roughly where it is (for example "drop zone ahead-left, about 3 m"). Your earlier sentences are shown back to you with the position and heading you had, so after a pickup you can head straight back to the zone instead of searching again. A drop zone is a marked area you could set something down on, not a painted line. The object lands just below the bottom of the image, where you cannot see, so drop only when the zone fills the whole bottom edge of the image, from the left corner to the right corner. If any part of the bottom edge is still bare floor you are at the zone's border and the drop will miss: move 0.4 m forward and look again.
6. If a move or turn reports collided=true, never repeat it. After a blocked move, back up or turn 30-60 degrees away and take a different path. After a blocked turn (a carried object swings wide), turn the other way round instead, or back up 0.3 m first.
7. Be efficient. Aim to finish in about 25 steps; the run is stopped at 60.
""" + GO_TO_HINT + """
Each step, write one short sentence saying what you see and why you chose your action, then call exactly one tool."""


def load_env(path=None):
    """Read KEY=VALUE lines from the git-ignored .env next to the repo root into os.environ (no overwrite)."""
    path = Path(path) if path else Path(__file__).resolve().parents[1] / ".env"
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        name, sep, value = line.strip().partition("=")
        if sep and name and not name.startswith("#"):
            os.environ.setdefault(name.strip(), value.strip().strip('"').strip("'"))


def claude_key():
    """The Claude key from .env or the shell. CLAUDE_API_KEY is accepted as another name for ANTHROPIC_API_KEY."""
    load_env()
    return os.getenv("ANTHROPIC_API_KEY") or os.getenv("CLAUDE_API_KEY")


def require_api_key():
    key = claude_key()
    if key:
        os.environ["ANTHROPIC_API_KEY"] = key
    if not key:
        raise SystemExit("ANTHROPIC_API_KEY is not set. Put ANTHROPIC_API_KEY=<your key> in E:/ansu-hack/.env "
                         "(git-ignored) or set it in this shell, then run again.")


# The warehouse scene's robot carries items on a lifting platform instead of a front gripper, and has
# no /go_to. Its prompt and tools describe that robot only: nothing about the room or what is in it.
LIFT_TOOLS = [
    {"name": "move", "description": "Drive straight. distance is in metres, -1 to 1; negative drives backward. Speed 1 m/s. Stops early if the robot touches something; the result then says collided=true and the robot has backed off 0.1 m.",
     "input_schema": {"type": "object", "properties": {"distance": {"type": "number", "minimum": -1, "maximum": 1}}, "required": ["distance"]}},
    {"name": "turn", "description": "Turn in place. degrees is -180 to 180; positive turns right (clockwise seen from above), negative turns left. Speed 90 deg/s.",
     "input_schema": {"type": "object", "properties": {"degrees": {"type": "number", "minimum": -180, "maximum": 180}}, "required": ["degrees"]}},
    {"name": "pickup", "description": "Lift the item directly ahead onto the robot's carrying platform. Works when the item is roughly centred in the image and close: within about 1.5 m of the robot's centre (about 1 m from the camera) and within 30 degrees of straight ahead. If items are stacked, it takes the top one. Returns ok=true or ok=false only; it never says what was taken. A carried item rides on top of the robot, out of the camera's view.",
     "input_schema": {"type": "object", "properties": {}}},
    {"name": "drop", "description": "Set the carried item down about 1.2 m in front of the robot. Returns ok and in_zone (whether it landed in the drop zone). The run ends when everything the command asked for has been delivered, or on a wrong or misplaced delivery.",
     "input_schema": {"type": "object", "properties": {}}},
    {"name": "fail", "description": "Give up and end the run when the task cannot be completed.",
     "input_schema": {"type": "object", "properties": {"reason": {"type": "string"}}, "required": ["reason"]}},
]

SYSTEM_LIFT = """You drive a wheeled robot that carries items on a lifting platform on its top. A user gave it one plain-language fetch command: find what was asked for, pick it up, carry it to the marked drop zone, and set it down there. A command may ask for more than one item; then repeat until all are delivered.

What you can sense:
- One forward camera image (640x480, 90 degree horizontal field of view), mounted 0.5 m above the floor at the front of the robot and tilted 10 degrees down. This image is your only source of information about the world. You get a new image only after a move or turn. You cannot see the item you are carrying.
- depth_m: distance in metres along the centre of the camera view to the nearest thing, or none if nothing is within 10 m. Because the camera looks slightly down, open floor ahead reads about 2.9; a smaller number means something is closer than that. It does not say what the thing is.
- Position: your own x, y in metres and heading in degrees (0 = +x axis, increasing clockwise seen from above). It says nothing about where anything else is.
- Robot state: whether the robot is carrying something and how many collisions have happened (3 collisions end the run as a failure).

How to work, one tool call per step:
1. Read the command carefully: which colour, which shape, how many. Commands can need reasoning, for example "the tallest one". Decide what fits by comparing what you see. A command that compares (tallest, biggest, nearest to something) can only be answered once you have seen the whole room: finish the full circle before choosing. Some things in view may be fixtures or stored goods that the robot cannot take. When several things could fit the command, prefer one standing by itself on open floor, and treat a pickup that fails twice at close range as proof that thing is not the target.
2. To search, turn exactly 45 degrees, always in the same direction, and look again. Eight looks cover a full circle; the status lists the headings you have already viewed from this spot, so never view one twice. If something in view clearly fits the command, go to it at once. Otherwise finish the circle before driving anywhere, noting each possible candidate and its heading in your sentence; then check the candidates, nearest first. Never drive off toward something you cannot identify while part of the circle is still unseen. If nothing fits after a full circle, drive 1-2 m toward open floor and search again. If nothing fits after full circles from three spots, call fail and say what you saw instead.
3. Steer so the target is in the middle of the image. A target left of centre needs a negative (left) turn; right of centre needs a positive turn. With a 90 degree field of view, a target at the left or right image edge is about 45 degrees off, and one halfway between the centre and an edge about 25. Turn by the whole offset in one go. A target inside the middle third of the image is centred enough: do not turn, drive, and correct only if it leaves the middle third. Never follow a turn with a turn back the other way.
4. Approach in steps: moves of up to 1 m while far, then 0.2-0.4 m when close, re-centring with small turns. When the target is centred and large in the image, with depth_m around 1.2 or less, call pickup. Do not drive into it: touching anything counts as a collision.
5. Whenever the drop zone is in view, at any point in the run, say so in your sentence with roughly where it is. Your earlier sentences are shown back to you with the position and heading you had, so after a pickup you can head straight back to it instead of searching again. After a successful pickup, drive toward the drop zone. Stop when its near edge reaches the bottom edge of the image, then call drop; the item is set down about 1.2 m ahead. Do not bump into the drop zone itself.
6. If a move reports collided=true, do not repeat it. Back up (negative move) or turn 30-60 degrees away, then take a different path.
7. Be efficient. Aim for about 25 steps per item; the run is stopped at 60 steps.

Each step, write one short sentence saying what you see and why you chose your action, then call exactly one tool."""


# Only the latest image is ever sent, so the model's own one-line note per view is its whole memory of
# earlier views. Making it a required tool argument stops it being skipped.
NOTE = {"type": "string", "description": "One short line recording this view for later steps: each object you can see with its "
        "side (left/centre/right), rough distance and anything the command cares about (colour, shape, relative height), and the "
        "drop zone if visible. Write 'nothing new' only if the image is unchanged."}
for _tool in TOOLS + LIFT_TOOLS:
    if _tool["name"] != "fail":
        _tool["input_schema"]["properties"]["note"] = NOTE
        _tool["input_schema"]["required"] = _tool["input_schema"].get("required", []) + ["note"]


def detect_robot(engine):
    """'gripper' for the arena engine, 'lift' for the warehouse engine (it has no display-camera endpoint)."""
    try:
        engine.call("/view")
        return "gripper"
    except HTTPError:
        return "lift"
    except Exception:
        return "gripper"


def find_engine_url():
    """Which engine to drive. ROBOHALO_ENGINE_URL wins; otherwise the TurtleBot arena engine on 8080 or
    8081 (it moves to 8081 when the warehouse project holds 8080); otherwise whatever is on 8080."""
    if os.getenv("ROBOHALO_ENGINE_URL"):
        return os.environ["ROBOHALO_ENGINE_URL"].rstrip("/")
    for port in (8080, 8081):
        try:
            with urlopen(Request(f"http://127.0.0.1:{port}/view"), timeout=1.5) as response:
                if response.status == 200:
                    return f"http://127.0.0.1:{port}"   # only the arena engine has /view
        except Exception:
            continue
    return "http://127.0.0.1:8080"


class Engine:
    def __init__(self, base=None):
        self.base = (base or find_engine_url()).rstrip("/")

    def call(self, path, body=None, timeout=30):
        data = None if body is None else json.dumps(body).encode()
        request = Request(self.base + path, data=data, headers={"Content-Type": "application/json"})
        with urlopen(request, timeout=timeout) as response:
            content = response.read()
            kind = response.headers.get_content_type()
            return json.loads(content) if kind == "application/json" or kind == "text/plain" else content   # images and audio stay bytes


NL = "\n"


class RefusalError(RuntimeError):
    pass


def search_note(viewed, spots):
    """Which headings the camera has already covered from where the robot stands (its own odometry only)."""
    if len(viewed) < 2:
        return None
    headings = sorted({round(h) % 360 for h in viewed})
    gaps = [(b - a) % 360 or 360 for a, b in zip(headings, headings[1:] + headings[:1])]
    note = f"Headings already viewed from this spot: {', '.join(map(str, headings))} (each view spans 45 deg either side)."
    if max(gaps) <= 90:  # 90 degree field of view: no gap left between neighbouring views
        note += (f" That is the full circle from search spot {spots}. Do not turn to search here again: "
                 "drive toward the best candidate, or 1-2 m toward open floor and search from there")
        note += ", or call fail if nothing you have seen fits the command." if spots >= 3 else "."
    return note


def status_text(step, depth, state, has_image):
    lines = [
        f"Step {step} of {MAX_STEPS} (aim to finish within {SOFT_STEPS}).",
        f"Holding: {'yes' if state.get('holding') else 'no'}. Collisions so far: {state.get('retry_count', 0)} of 3.",
        f"depth_m: {'none' if depth is None else round(depth, 2)}",
    ]
    if "x" in state:
        lines.insert(1, f"Position: x={state['x']:.2f}, y={state['y']:.2f}, heading={state.get('heading', 0):.0f} deg")
    note = search_note(state.get("viewed") or [], state.get("spots", 1))
    if note:
        lines.append(note)
    if state.get("blocked"):
        lines.append(f"Your last {state['blocked']} collided. Do not repeat it: " + (
            "turn the other way round, or back up 0.3 m first." if state["blocked"] == "turn" else "back up or steer around the obstacle."))
    if state.get("pickup_failed"):
        lines.append("The pickup failed, so the target is not within reach yet. Do not call pickup again from this pose: "
                     "move about 0.1 m closer or re-centre first. The image is the same view as before.")
    if state.get("pickup_fails", 0) >= 2:
        lines.append(f"{state['pickup_fails']} pickups in a row have failed. If it was centred with depth_m at 0.25 or less, this thing "
                     "cannot be picked up, so it is not what the command means: leave it and go to the next best candidate in your notes.")
    if state.get("dithering"):
        lines.append("Your last two turns were small and cancelled each other out. The target is centred enough: do not turn again, drive toward it now.")
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
        context += ["Earlier steps:"] + earlier[-24:]  # long enough to still hold the first sweep after a pickup
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


def request_kwargs(model, messages, tools=None, system=None):
    tools = tools or TOOLS
    if system is None:
        system = SYSTEM if any(t["name"] == "go_to" for t in tools) else SYSTEM.replace(GO_TO_HINT, "")
    kwargs = {
        "model": model,
        "max_tokens": 1024,  # thinking counts toward max_tokens
        "system": [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],  # tools + system cached
        "tools": tools,
        "tool_choice": {"type": "auto", "disable_parallel_tool_use": True},  # forced tool_choice is a 400 on Sonnet 5.5
        "messages": messages,
        "extra_body": {"output_config": {"effort": "low"}},  # short per-step decisions; keeps thinking tokens low
    }
    if model in FALLBACK_MODELS:
        kwargs["betas"] = ["server-side-fallback-2026-07-01"]
        kwargs["extra_body"]["fallbacks"] = "default"
    return kwargs


def choose_tool(client, model, command, frame, depth, state, history, step=1, previous=None, tools=None, system=None):
    """One Claude call. Returns (tool name or None, args, usage, reasoning text, tool_use id)."""
    messages = build_messages(command, frame, depth, state, history, step, previous)
    response = client.beta.messages.create(**request_kwargs(model, messages, tools, system))
    if getattr(response, "stop_reason", None) == "refusal":
        raise RefusalError(str(getattr(response, "stop_details", "") or "refused"))
    calls = [b for b in response.content if getattr(b, "type", None) == "tool_use"]
    reasoning = " ".join(b.text.strip() for b in response.content if getattr(b, "type", None) == "text" and b.text.strip())
    usage = getattr(response, "usage", None)
    if not calls:
        return None, {}, usage, reasoning, None
    return calls[0].name, dict(calls[0].input), usage, reasoning, calls[0].id


def validate(name, args):
    if name not in {tool["name"] for tool in TOOLS + LIFT_TOOLS} or not isinstance(args, dict):
        raise ValueError("Unknown or malformed tool call")
    if name == "move" and not -1 <= float(args.get("distance", 99)) <= 1:
        raise ValueError("Move must be between -1 and 1 metre")
    if name == "go_to" and not all(isinstance(args.get(k), (int, float)) for k in ("x", "y")):
        raise ValueError("go_to needs numeric x and y")
    if name == "turn" and not -180 <= float(args.get("degrees", 999)) <= 180:
        raise ValueError("Turn must be between -180 and 180 degrees")


def step_cost(model, usage):
    if usage is None:
        return 0.0
    if not str(model).startswith("claude"):
        return 0.0  # free-tier test providers
    price = PRICES.get(model, PRICES[DEFAULT_MODEL])
    tokens = [getattr(usage, k, 0) or 0 for k in ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens", "output_tokens")]
    return sum(t * p for t, p in zip(tokens, price)) / 1_000_000


def history_line(step, name, args, result):
    arg = ", ".join(f"{k}={v}" for k, v in args.items())
    keep = {k: result[k] for k in ("ok", "collided", "moved", "heading", "in_zone", "reached", "error") if k in result}
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


def run(command, client, model, engine=None, runs_dir="runs", max_steps=MAX_STEPS, record=False, allow_go_to=True, robot=None, on_step=None):
    engine = engine or Engine()
    robot = robot or detect_robot(engine)
    if robot == "lift":
        tools, system = LIFT_TOOLS, SYSTEM_LIFT
    else:
        tools, system = (TOOLS if allow_go_to else [t for t in TOOLS if t["name"] != "go_to"]), None
    engine = engine or Engine()
    run_dir = Path(runs_dir) / (datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6])
    run_dir.mkdir(parents=True)
    run_number = count_run(runs_dir) if str(model).startswith("claude") else 0  # only Claude runs count toward the budget
    log_path = run_dir / "run.jsonl"
    history, cost = [], 0.0
    tokens = {"input": 0, "cache_write": 0, "cache_read": 0, "output": 0}
    start_body = {"command": command}
    if record:
        start_body["record_dir"] = str((run_dir / "clip_frames").resolve())
    if engine.call("/state").get("state") != "Idle":
        engine.call("/reset", {})  # a run the agent ended itself (hard stop, fail, API error) leaves the engine mid-run
    engine.call("/start", start_body)
    need_look, frame, frame_name = True, None, None
    outcome, step, previous = None, 0, None
    viewed, spots = [], 1  # headings looked at from the current spot, and how many spots were searched
    turns, resend = [], False  # turns in a row (to catch left-right dithering); show the last frame again
    pickup_fails = 0           # failed pickups since the last one that worked

    with log_path.open("w", encoding="utf-8") as log:
        for step in range(1, max_steps + 1):
            sent = None
            if need_look:
                frame = engine.call("/look")
                frame_name = f"frame-{step:03}.png"
                save_png(frame, run_dir / frame_name)
                sent = frame
            elif resend:
                sent = frame  # a failed pickup or drop: same view, no new /look, so the model is not left blind
            state = engine.call("/state")
            depth = engine.call("/depth")
            if sent is not None and "heading" in state:
                viewed.append(state["heading"])
            state["viewed"], state["spots"] = viewed, spots
            state["pickup_failed"] = resend and bool(previous) and previous["name"] == "pickup"
            state["pickup_fails"] = pickup_fails
            state["blocked"] = previous["name"] if previous and previous["result"].get("collided") else None
            state["dithering"] = len(turns) >= 2 and turns[-1] * turns[-2] < 0 and max(abs(turns[-1]), abs(turns[-2])) <= 20
            name, args, reasoning, usage, call_id = None, {}, "", None, None
            try:
                name, args, usage, reasoning, call_id = choose_tool(client, model, command, sent, depth, state, history, step, previous, tools, system)
            except RefusalError as error:
                result, outcome = {"ok": False, "error": f"model refused: {error}"}, "fail_not_reached"
            except Exception as error:  # API failure after the SDK's own retries
                result, outcome = {"ok": False, "error": f"API error: {error}"}, "fail_not_reached"
            else:
                note = str(args.pop("note", "") or "")  # memory only: never sent to the engine
                reasoning = " | ".join(part for part in (reasoning, note) if part)
                if name is None:
                    result = {"ok": False, "error": "no valid tool call; call exactly one tool with all its required arguments"}
                elif name == "fail":
                    result, outcome = {"ok": False, "reason": args.get("reason", "")}, "fail_not_reached"
                else:
                    try:
                        validate(name, args)
                        result = call_engine(engine, "/" + name, args if name in ("move", "turn", "go_to") else {})
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
            if on_step:
                on_step(record_line)  # live progress for the chat box
            # The model's own one-line observation and where it stood: its only memory of earlier views.
            pose = f" [at x={state['x']:.1f} y={state['y']:.1f} heading={state.get('heading', 0):.0f}]" if "x" in state else ""
            history.append(history_line(step, name, args, result) + pose + (f" saw: {reasoning[:220]}" if reasoning else ""))
            previous = {"id": call_id, "name": name, "input": args, "result": result} if call_id else None
            # Look again only after the robot actually moved or turned.
            need_look = name in ("move", "turn", "go_to") and not result.get("error")
            resend = name in ("pickup", "drop") and not result.get("ok")
            if name == "pickup":
                pickup_fails = 0 if result.get("ok") else pickup_fails + 1
            turns = turns + [float(args.get("degrees") or 0)] if name == "turn" else []
            if name == "go_to" or (name == "move" and abs(result.get("moved") or 0) >= 0.5):
                viewed, spots = [], spots + 1  # a new vantage point: earlier views no longer cover it
            elif name in ("pickup", "drop") and result.get("ok"):
                viewed, spots = [], 1          # now looking for something else (the drop zone, or the next item)
            if outcome:
                break
        if not outcome:
            outcome = "fail_not_reached"  # hard stop at max_steps, no exceptions
        final_state = call_engine(engine, "/state")
        summary = {"command": command, "outcome": outcome, "step_count": step, "retries": final_state.get("retry_count"),
                   "model": model, "tokens": tokens, "cost_estimate_usd": round(cost, 6), "run_number": run_number,
                   "run_dir": str(run_dir), "robot": robot}
        if getattr(client, "limits", None):
            summary["rate_limit"] = client.limits  # what the free tier has left (test providers)
        if record:
            try:
                summary["clip"] = finish_clip(engine, run_dir, command)
            except Exception as error:
                summary["clip"] = {"ok": False, "problems": [str(error)]}
        log.write(json.dumps({"summary": summary}, separators=(",", ":")) + "\n")
    try:
        from agent.replay import write_replay
        write_replay(run_dir)  # replay.html next to the log, for reviewers
    except Exception as error:
        print("replay page not written:", error)
    return summary


GROQ_TEST_STEPS = 25  # keep free-tier test runs short


# Free-tier stand-ins that speak the OpenAI chat format: (key variable, base URL, model variable).
OPENAI_COMPATIBLE = {
    "groq": ("GROQ_API_KEY", "https://api.groq.com/openai/v1", "GROQ_MODEL"),
    "gemini": ("GEMINI_API_KEY", "https://generativelanguage.googleapis.com/v1beta/openai", "GEMINI_MODEL"),
}


def pick_gemini_model(key, base):
    """Newest general-purpose Flash model on the key's live list (set GEMINI_MODEL to choose another)."""
    request = Request(base + "/models", headers={"Authorization": "Bearer " + key, "User-Agent": "robohalo-agent/1.0"})
    with urlopen(request, timeout=20) as response:
        ids = [m["id"] for m in json.loads(response.read()).get("data", [])]
    skip = ("lite", "image", "tts", "live", "audio", "embedding", "aqa", "exp", "thinking", "robotics", "computer")
    flash = sorted(i for i in ids if "flash" in i and not any(word in i for word in skip))
    if not flash:
        raise SystemExit("No Gemini Flash model on this key. Set GEMINI_MODEL to one of: " + ", ".join(sorted(ids)[:20]))
    stable = [i for i in flash if "preview" not in i] or flash
    return stable[-1].split("/")[-1]


def make_client(provider=None, model=None):
    """Returns (client, model, provider). Claude is the default; the others are stand-ins until a Claude key arrives."""
    load_env()
    if provider is None:
        provider = ("claude" if claude_key() else "gemini" if os.getenv("GEMINI_API_KEY")
                    else "groq" if os.getenv("GROQ_API_KEY") else "claude")
    if provider in OPENAI_COMPATIBLE:
        from agent.groq_client import GROQ_MODEL, GroqClient
        key_name, base, model_name = OPENAI_COMPATIBLE[provider]
        key = os.getenv(key_name)
        if not key:
            raise SystemExit(f"{key_name} is not set. Put {key_name}=<your key> in E:/ansu-hack/.env (git-ignored).")
        model = model or os.getenv(model_name) or (GROQ_MODEL if provider == "groq" else pick_gemini_model(key, base))
        print(f"Provider: {provider} ({model}). Stand-in for testing: judged runs must use Claude.")
        return GroqClient(key, url=base + "/chat/completions", min_interval=2.5 if provider == "groq" else 0.5), model, provider
    return claude_client(), model or os.getenv("CLAUDE_MODEL", DEFAULT_MODEL), provider


def claude_client():
    """Anthropic client that uses only the project's key and the official endpoint.

    A shell can carry its own ANTHROPIC_BASE_URL or ANTHROPIC_AUTH_TOKEN (for example when this runs
    inside another tool). Those must not redirect the project's calls or ride along with them.
    """
    require_api_key()
    from anthropic import Anthropic
    os.environ.pop("ANTHROPIC_AUTH_TOKEN", None)   # this process only
    return Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"], base_url=os.getenv("ROBOHALO_ANTHROPIC_BASE_URL", "https://api.anthropic.com"))


def add_provider_args(parser):
    parser.add_argument("--provider", choices=["claude", "gemini", "groq"], help="default: claude if its key is set, else gemini, else groq")
    parser.add_argument("--model", help="model id (default: claude-sonnet-5-5, or the Groq test model)")
    parser.add_argument("--max-steps", type=int, help=f"default {MAX_STEPS} for Claude, {GROQ_TEST_STEPS} for Groq")
    parser.add_argument("--no-record", action="store_true", help="do not record the POV clip")
    parser.add_argument("--no-go-to", action="store_true", help="move and turn only (stretch goal: Claude plans the whole path)")


def run_from_args(command, args, engine=None, on_step=None):
    client, model, provider = make_client(args.provider, args.model)
    steps = args.max_steps or (MAX_STEPS if provider == "claude" else GROQ_TEST_STEPS)
    return run(command, client, model, engine, max_steps=min(steps, MAX_STEPS), record=not args.no_record, allow_go_to=not args.no_go_to, on_step=on_step)


def serve_panel(engine, args):
    """Serve the Unity panel: a typed (Send) or spoken (Speak) request goes to Claude, Claude decides which
    item it means from the camera views, and the engine's own test-run routine picks it up and drops it (agent.speak)."""
    from types import SimpleNamespace
    from agent import speak
    from agent.scout import voice_command

    def tell(text):
        call_engine(engine, "/agent_status", {"command": text})  # the line under the command box, where the engine has one

    while True:
        try:
            waiting = engine.call("/agent_command")
            command = waiting.get("command")
            if not command and waiting.get("audio"):
                command = voice_command(SimpleNamespace(engine=engine))  # None if no words could be made out
            if not command:
                time.sleep(0.5)
                continue
            client, model, _ = make_client(args.provider, args.model)
            print(json.dumps(speak.handle(command, client, model, engine, tell), indent=2), flush=True)
        except KeyboardInterrupt:
            break
        except Exception as error:  # Unity restarting or a failed run must not kill the listener
            print(f"Listener: {error}", flush=True)
            time.sleep(2)


def main():
    parser = argparse.ArgumentParser(description="A vision model drives the robot from camera images.")
    parser.add_argument("command", nargs="?")
    parser.add_argument("--listen", action="store_true", help="run each command sent from Unity's AI COMMAND field")
    parser.add_argument("--port", type=int, default=8080, help="Unity HTTP port (default 8080)")
    add_provider_args(parser)
    args = parser.parse_args()
    if not args.listen and not args.command:
        parser.error("give a command or --listen")
    engine = Engine(f"http://127.0.0.1:{args.port}")
    if not args.listen:
        print(json.dumps(run_from_args(args.command, args, engine), indent=2))
        return
    make_client(args.provider, args.model)  # fail now, not on the first command, if no key
    print(f"Waiting for Unity AI COMMAND > Send at http://127.0.0.1:{args.port}/ ...", flush=True)
    serve_panel(engine, args)


if __name__ == "__main__":
    main()

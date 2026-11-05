"""Scout: a camera-only fetch agent that finishes on a slow, free vision model.

The model never drives. It only answers "what do you see in this camera image?": is the requested
item in view and where, is the drop zone in view and where. Ordinary robot code turns those answers
into motion, using the engine's /go_to path-finder (allowed by the brief), a depth sweep to range the
item, and the camera again to line up on the drop zone. A whole fetch needs about 6 to 10 model
calls instead of 25 to 60, so it completes in a few minutes on Groq's free tier.

Everything the model knows about objects still comes from /look pixels. Nothing from the engine about
objects is ever sent to it.

  python -m agent.scout "Bring the red cube to the green square"
  python -m agent.scout --listen --port 8081        # serve the Unity panel's AI COMMAND box
"""

import argparse
import base64
import json
import math
import sys
import time
import traceback
import uuid
from datetime import datetime, timezone
from pathlib import Path

from agent.run_agent import Engine, call_engine, make_client, save_png

CAM_FORWARD = 0.28      # camera is this far ahead of the robot centre (m)
CAM_HEIGHT = 0.5        # m
CAM_TILT = 10.0         # degrees down
HALF_HFOV_TAN = 1.0     # tan(45 deg): 90 degree horizontal field of view
HALF_VFOV_TAN = 0.75    # tan(36.87 deg): 4:3 image
OBJECT_RADIUS = 0.2     # typical half-width of an item (m)
DEPTH_RADIUS = 0.12     # the depth ray is a 0.12 m sphere cast

SYSTEM = """You are the eyes of a small wheeled fetch robot. Each message has one image from its forward camera (90 degree horizontal field of view; the robot's own gripper shows at the bottom of the image, and a carried item is lifted out of view) and the operator's request. The robot has to pick up the requested item and deliver it to the marked drop zone, which is a flat marked area on the floor.

Look only at this image. Judge the requested item by the colour, shape and size you can see. Requests can be indirect, for example "the tallest one" or "something to drink"; pick the best match among the things in view. If nothing in view fits, answer no. Never guess about things that are not in the image.

Answer with exactly these lines and nothing else:
NOTE: one short sentence saying what you see
TARGET: yes or no (is the requested item in the image?)
TARGET_X: horizontal position of the item's centre, from 0 (left edge) to 1 (right edge); 0.5 is the middle
ZONE: yes or no (is the marked drop zone in the image?)
ZONE_X: horizontal position of the centre of the visible part of the drop zone, 0 to 1
ZONE_Y: vertical position of that centre, from 0 (top edge) to 1 (bottom edge)
ZONE_UNDER_ROBOT: yes or no (does the drop zone reach the bottom edge of the image on both the left side and the right side?)"""

PANORAMA_SYSTEM = """You are the eyes of a small wheeled fetch robot. The image is a sheet of 8 numbered camera views, taken by turning the robot through a full circle with 45 degrees between views, so neighbouring views overlap. The robot has to pick up the requested item and deliver it to the marked drop zone, which is a flat marked area on the floor.

Compare everything visible across all 8 views. Judge the requested item by the colour, shape and size you can see. Requests can be indirect or comparative, for example "the tallest one" or "something to drink": pick the single best match on the whole sheet. When the item shows in two neighbouring views, choose the one where it is nearer the middle. Never guess about things that are not in the image.

Answer with exactly these lines and nothing else:
NOTE: one short sentence saying what the chosen view shows and why it is the best match
TARGET_VIEW: the number (1-8) of the view that shows the requested item best, or 0 if none does
ZONE_VIEW: the number (1-8) of the view that shows the marked drop zone best, or 0 if none does"""

_FIELDS = {"TARGET": ("target_visible", "bool"), "TARGET_X": ("target_x", "num"), "ZONE": ("zone_visible", "bool"), "ZONE_X": ("zone_x", "num"),
           "ZONE_Y": ("zone_y", "num"), "ZONE_UNDER_ROBOT": ("zone_under_robot", "bool"), "TARGET_VIEW": ("target_view", "int"),
           "ZONE_VIEW": ("zone_view", "int"), "NOTE": ("note", "text")}


def parse_answer(text):
    """'TARGET: yes' style lines -> dict. Unknown or malformed lines are ignored."""
    import re
    report = {}
    for line in str(text).splitlines():
        match = re.match(r"\s*[-*]?\s*([A-Z_]+)\s*[:=]\s*(.+?)\s*$", line.replace("**", ""))
        if not match or match.group(1) not in _FIELDS:
            continue
        key, kind = _FIELDS[match.group(1)]
        value = match.group(2)
        if kind == "bool":
            word = value.strip().lower()
            if word.startswith(("yes", "true")):
                report[key] = True
            elif word.startswith(("no", "false")):
                report[key] = False
        elif kind == "text":
            report[key] = value
        else:
            number = re.search(r"-?\d+(?:\.\d+)?", value)
            if number:
                report[key] = int(float(number.group())) if kind == "int" else float(number.group())
    return report


def make_sheet(frames):
    """8 camera frames -> one JPEG sheet, 4 x 2, each view numbered."""
    import io
    from PIL import Image, ImageDraw, ImageFont
    tile_w, tile_h = 480, 360
    sheet = Image.new("RGB", (tile_w * 4, tile_h * 2), (0, 0, 0))
    draw = ImageDraw.Draw(sheet)
    try:
        font = ImageFont.truetype("arialbd.ttf", 44)
    except OSError:
        font = ImageFont.load_default()
    for i, frame in enumerate(frames):
        x, y = (i % 4) * tile_w, (i // 4) * tile_h
        sheet.paste(Image.open(io.BytesIO(frame)).convert("RGB").resize((tile_w, tile_h)), (x, y))
        draw.rectangle((x, y, x + tile_w - 1, y + tile_h - 1), outline=(255, 255, 255), width=2)
        draw.rectangle((x + 6, y + 6, x + 62, y + 60), fill=(0, 0, 0))
        draw.text((x + 20, y + 8), str(i + 1), fill=(255, 255, 255), font=font)
    out = io.BytesIO()
    sheet.save(out, "JPEG", quality=88)
    return out.getvalue()


def pick_segment(readings, jump=0.3, far=8.0):
    """readings: [(offset_degrees, depth or None)] from a sweep. Splits them into runs of similar depth,
    keeps the runs that stand in front of their neighbours (things, not the wall behind), and returns
    (centre offset, nearest depth) of the one closest to the middle of the sweep."""
    segments, current = [], []
    for offset, depth in readings:
        solid = depth is not None and depth < far
        if current and (not solid or abs(depth - current[-1][1]) > jump):
            segments.append(current)
            current = []
        if solid:
            current.append((offset, depth))
        elif current:
            segments.append(current)
            current = []
    if current:
        segments.append(current)
    if not segments:
        return None
    nearest = [min(d for _, d in seg) for seg in segments]

    def in_front(i):
        left = nearest[i - 1] if i > 0 and segments[i - 1][-1][0] + 1.6 >= segments[i][0][0] else None
        right = nearest[i + 1] if i + 1 < len(segments) and segments[i][-1][0] + 1.6 >= segments[i + 1][0][0] else None
        return all(n is None or nearest[i] < n for n in (left, right))

    candidates = [i for i in range(len(segments)) if in_front(i)] or list(range(len(segments)))
    best = min(candidates, key=lambda i: abs(sum(o for o, _ in segments[i]) / len(segments[i])))
    return sum(o for o, _ in segments[best]) / len(segments[best]), nearest[best]


def bearing_from_x(x):
    """Image x (0..1) -> degrees to turn (positive = right)."""
    return math.degrees(math.atan((min(max(float(x), 0.0), 1.0) - 0.5) * 2 * HALF_HFOV_TAN))


def ground_distance_from_y(y):
    """Image y (0..1, floor point) -> distance ahead of the robot centre in metres, or None if at/above the horizon."""
    depression = CAM_TILT + math.degrees(math.atan((min(max(float(y), 0.0), 1.0) - 0.5) * 2 * HALF_VFOV_TAN))
    if depression <= 2.0:
        return None
    return CAM_HEIGHT / math.tan(math.radians(depression)) + CAM_FORWARD


def ahead(state, distance, offset_degrees=0.0):
    """World point `distance` metres from the robot centre along its heading (+ offset)."""
    h = math.radians(state["heading"] + offset_degrees)
    return state["x"] + distance * math.cos(h), state["y"] - distance * math.sin(h)


class Scout:
    def __init__(self, client, model, engine=None, runs_dir="runs", say=print, max_model_calls=16, use_sheet=None):
        # The full-circle sheet saves model calls and handles comparative requests ("the tallest one"),
        # but small models misread its small views, so it is off for them unless asked for.
        self.use_sheet = use_sheet if use_sheet is not None else not str(model).lower().startswith(("qwen", "meta-llama", "llama"))
        self.client, self.model, self.engine, self.say = client, model, engine or Engine(), say
        self.runs_dir, self.max_model_calls = runs_dir, max_model_calls
        if hasattr(client, "step_tokens"):
            client.step_tokens = 2900   # one perception call is about 2,500 tokens

    # ---------- plumbing ----------
    def tell(self, text):
        self.say(text)
        call_engine(self.engine, "/agent_status", {"command": text})  # shows in the Unity panel when supported

    def act(self, tool, args=None, timeout=90, why=""):
        try:
            result = self.engine.call("/" + tool, args or {}, timeout) if tool == "go_to" else call_engine(self.engine, "/" + tool, args or {})
        except Exception as error:
            result = {"ok": False, "error": str(error)}
        self.record(tool, args or {}, result, None, why)
        return result

    def record(self, tool, args, result, frame_name, why):
        self.step += 1
        line = {"step": self.step, "timestamp": datetime.now(timezone.utc).isoformat(), "tool": tool, "args": args,
                "result": result, "frame_path": frame_name, "reasoning": why}
        self.log.write(json.dumps(line, separators=(",", ":")) + "\n")
        self.log.flush()

    def state(self):
        return self.engine.call("/state")

    def ask(self, system, image, text, need):
        """One image + one question -> the model's answer as a dict. Asks once more if the `need` field is missing."""
        report = {}
        for _ in range(2):
            if self.model_calls >= self.max_model_calls:
                raise RuntimeError("model call budget used up")
            self.model_calls += 1
            response = self.client.beta.messages.create(
                model=self.model, max_tokens=400, system=[{"type": "text", "text": system}],
                messages=[{"role": "user", "content": [
                    {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": base64.b64encode(image).decode()}},
                    {"type": "text", "text": text}]}])
            answer = " ".join(b.text for b in response.content if getattr(b, "type", None) == "text")
            report = parse_answer(answer)
            if need in report:
                break
        return report

    def perceive(self, purpose):
        """One camera frame -> one model call -> the report dict."""
        frame = self.engine.call("/look")
        frame_name = f"frame-{self.step + 1:03}.png"
        save_png(frame, self.run_dir / frame_name)
        holding = bool(self.state().get("holding"))
        text = (f'Operator request: "{self.command}"\n'
                + ("The robot is carrying the item now. Report the drop zone." if holding else "The robot is looking for the requested item. Also report the drop zone if you can see it."))
        report = self.ask(SYSTEM, frame, text, "zone_visible" if holding else "target_visible")
        self.record("look", {"purpose": purpose}, {k: v for k, v in report.items() if k != "note"}, frame_name, str(report.get("note", "")))
        return report

    def turn_to(self, heading, why):
        delta = (heading - self.state()["heading"] + 180) % 360 - 180
        if abs(delta) > 0.5:
            self.act("turn", {"degrees": round(delta, 1)}, why=why)

    # ---------- behaviour ----------
    def find_target(self):
        """Full-circle sheet of 8 views -> the model picks the best view -> one close look to locate the item in it."""
        self.zone_heading = None
        if not self.use_sheet:
            return self.sweep_for_target()
        start = self.state()["heading"]
        frames = []
        for _ in range(8):
            frames.append(self.engine.call("/look"))
            call_engine(self.engine, "/turn", {"degrees": 45})
        sheet = make_sheet(frames)
        sheet_name = f"frame-{self.step + 1:03}.png"
        save_png(sheet, self.run_dir / sheet_name)
        choice = self.ask(PANORAMA_SYSTEM, sheet, f'Operator request: "{self.command}"', "target_view")
        self.record("look", {"purpose": "full-circle sheet of 8 views"}, {k: v for k, v in choice.items() if k != "note"}, sheet_name, str(choice.get("note", "")))

        def view(key):
            try:
                number = int(choice.get(key) or 0)
            except (TypeError, ValueError):
                number = 0
            return number if 1 <= number <= 8 else 0

        if view("zone_view"):
            self.zone_heading = (start + (view("zone_view") - 1) * 45) % 360
        if view("target_view"):
            self.tell(f"Best match is in view {view('target_view')}: " + str(choice.get("note", "")).strip())
            self.turn_to((start + (view("target_view") - 1) * 45) % 360, "face the chosen view")
            report = self.perceive("locate")
            if report.get("target_visible"):
                return report
        self.tell("Checking each direction one at a time.")
        return self.sweep_for_target()

    def sweep_for_target(self):
        """Fallback: turn in 45 degree steps until the model sees the item."""
        for look in range(9):
            report = self.perceive("search")
            if report.get("zone_visible") and self.zone_heading is None and "zone_x" in report:
                self.zone_heading = (self.state()["heading"] + bearing_from_x(report["zone_x"])) % 360
            if report.get("target_visible"):
                self.tell("Found it: " + str(report.get("note", "")).strip())
                return report
            self.tell(f"Not in view ({look + 1}/9): turning to look further.")
            self.act("turn", {"degrees": 45}, why="search sweep")
        return None

    def range_target(self, report):
        """Face the item, then sweep the depth ray across it. Returns the distance to the thing on the
        model's bearing (not simply the nearest thing: a neighbour may be closer), or None."""
        self.act("turn", {"degrees": round(bearing_from_x(report.get("target_x", 0.5)), 1)}, why="face the item")
        half, step, count = 12.0, 1.5, 17
        self.act("turn", {"degrees": -half}, why="depth sweep start")
        readings = []
        for i in range(count):
            readings.append((-half + i * step, self.engine.call("/depth")))
            if i < count - 1:
                call_engine(self.engine, "/turn", {"degrees": step})
        chosen = pick_segment(readings)
        offset = chosen[0] if chosen else 0.0
        self.act("turn", {"degrees": round(offset - half, 1)}, why="depth sweep: face the thing on the item's bearing")
        return chosen[1] if chosen else None

    def approach(self, report):
        """Drive to the item the model located and check, with one more look, that it is the right one."""
        for attempt in range(2):
            depth = self.range_target(report)
            if depth is None:
                return "I could not measure the distance to the item."
            goal = ahead(self.state(), CAM_FORWARD + depth + DEPTH_RADIUS + OBJECT_RADIUS)
            self.tell(f"The item is about {depth:.1f} m away. Driving to it.")
            self.act("go_to", {"x": round(goal[0], 2), "y": round(goal[1], 2), "stop_short": 0.76}, why="approach the item by the shortest clear path")
            check = self.perceive("check before pickup")
            if check.get("target_visible") and abs(float(check.get("target_x", 0.5)) - 0.5) <= 0.2:
                return None
            if attempt == 1:
                return "The thing in front of me is not what was asked for."
            self.tell("That is not the requested item. Backing off to aim again.")
            self.act("move", {"distance": -0.8}, why="wrong thing ahead: back off")
            report = self.perceive("re-aim")
            if not report.get("target_visible"):
                return "I lost sight of the item."
        return None

    def fetch(self):
        report = self.find_target()
        if report is None:
            return "I looked all the way round and could not see anything that matches."
        problem = self.approach(report)
        if problem:
            return problem
        picked = self.act("pickup", why="the requested item is right in front").get("ok")
        for _ in range(4):
            if picked:
                break
            self.act("move", {"distance": 0.1}, why="edge closer")
            picked = self.act("pickup", why="retry").get("ok")
        if not picked:
            return "I reached the spot but could not pick anything up."
        self.tell("Picked it up. Looking for the drop zone.")
        return self.deliver()

    def deliver(self):
        if self.zone_heading is not None:   # seen during the search: face that way first
            turn = (self.zone_heading - self.state()["heading"] + 180) % 360 - 180
            self.act("turn", {"degrees": round(turn, 1)}, why="face where the drop zone was seen")
        misses = close = 0
        for _ in range(10):
            report = self.perceive("drop zone")
            if report.get("zone_under_robot") or close >= 2:   # over it, or two looks in a row put it under 1 m ahead
                break
            if not report.get("zone_visible"):
                misses += 1
                if misses > 8:
                    return "I could not find the drop zone."
                self.act("turn", {"degrees": 45}, why="search for the drop zone")
                continue
            angle = bearing_from_x(report.get("zone_x", 0.5))
            distance = ground_distance_from_y(report.get("zone_y", 0.5))
            # Far estimates from one image are rough, so close in by steps and look again each time.
            step = 1.5 if distance is None else min(max(distance - 0.6, 0.3), 1.5)
            close = close + 1 if distance is not None and distance < 1.0 else 0
            state = self.state()
            goal = ahead(state, step, angle)
            self.tell("Drop zone in view: moving closer.")
            self.act("go_to", {"x": round(goal[0], 2), "y": round(goal[1], 2)}, why=f"toward the drop zone ({angle:+.0f} deg, ~{step:.1f} m)")
            now = self.state()
            aim = (state["heading"] + angle - now["heading"] + 180) % 360 - 180   # go_to may leave the robot turned
            if abs(aim) > 5:
                self.act("turn", {"degrees": round(aim, 1)}, why="face the drop zone again")
        else:
            return "I could not line up on the drop zone."
        dropped = self.act("drop", why="front of the robot is over the drop zone")
        return None if dropped.get("ok") else "The drop failed."

    def run(self, command):
        self.command, self.step, self.model_calls = command, 0, 0
        self.run_dir = Path(self.runs_dir) / (datetime.now().strftime("%Y%m%d-%H%M%S") + "-scout-" + uuid.uuid4().hex[:6])
        self.run_dir.mkdir(parents=True)
        problem = None
        with (self.run_dir / "run.jsonl").open("w", encoding="utf-8") as self.log:
            started = call_engine(self.engine, "/start", {"command": command})
            if started.get("error"):   # e.g. still holding something from an abandoned run
                call_engine(self.engine, "/reset", {})
                started = call_engine(self.engine, "/start", {"command": command})
            if started.get("error"):
                problem = "The robot could not start: " + started["error"]
            else:
                self.tell(f'Working on: "{command}"')
                try:
                    problem = self.fetch()
                except Exception as error:
                    problem = f"Stopped: {error}"
            final = self.state()
            if final.get("state") != "Done":
                call_engine(self.engine, "/fail", {})
                final = self.state()
            outcome = final.get("outcome") or "fail_not_reached"
            summary = {"command": command, "outcome": outcome, "step_count": self.step, "model_calls": self.model_calls,
                       "retries": final.get("retries"), "model": self.model, "mode": "scout", "run_dir": str(self.run_dir), "problem": problem}
            self.log.write(json.dumps({"summary": summary}, separators=(",", ":")) + "\n")
        try:
            from agent.replay import write_replay
            write_replay(self.run_dir)
        except Exception:
            pass
        self.tell(("Done: delivered. " if outcome == "pass" else f"Not completed ({outcome}). " + (problem or "")) + f" [{self.model_calls} model calls]")
        return summary


def run_fast(scout, command):
    """Preset speed: one small text-only model call picks the basic commands, the engine's own planner drives.
    The model is told what is in the scene, so this is a demo mode and not a judged camera-only run."""
    from agent import quick
    scout.tell(f'Working on: "{command}"')
    record = quick.handle(command, scout.client, scout.model, scout.engine, runs_dir=scout.runs_dir, say=lambda line: scout.tell(line.strip()))
    results = [r["result"] for r in record["results"]]
    if not record["steps"]:
        scout.tell(record["note"] or "Nothing to run for that request.")
    else:
        scout.tell("Done." if all(r.startswith(("pass", "done")) for r in results) else "Finished with problems: " + "; ".join(results))
    return record


def voice_command(scout):
    """Fetch the Speak button's recording from Unity, transcribe it, and show the words in the panel."""
    from urllib.error import HTTPError
    from agent.transcribe import transcribe
    try:
        wav = scout.engine.call("/agent_audio")
    except HTTPError:
        return None
    text = transcribe(wav, say=lambda line: print(line, flush=True))
    print(f'Heard: "{text}"', flush=True)
    call_engine(scout.engine, "/agent_heard", {"command": text})
    return text or None


def listen(scout, poll=0.5, fast=False):
    print(f"Waiting for the Unity panel: type in AI COMMAND and press Send ({scout.engine.base})", flush=True)
    while True:
        try:
            waiting = scout.engine.call("/agent_command")
            command = waiting.get("command")
            if not command and waiting.get("audio"):
                command = voice_command(scout)
            if command:
                print(json.dumps(run_fast(scout, command) if fast else scout.run(command), indent=2), flush=True)
            else:
                time.sleep(poll)
        except KeyboardInterrupt:
            return
        except OSError as error:
            print(f"Waiting for Unity ({error})", flush=True)
            time.sleep(2)
        except Exception:
            traceback.print_exc()
            time.sleep(2)


def find_engine(port=None):
    """The arena engine listens on 8080, or 8081 when another Unity project holds 8080."""
    for candidate in ([port] if port else [8080, 8081]):
        engine = Engine(f"http://127.0.0.1:{candidate}")
        try:
            engine.call("/view" if port is None else "/state")   # only the arena engine has /view
            return engine
        except Exception:
            continue
    return Engine(f"http://127.0.0.1:{port or 8080}")


def main(argv=None):
    parser = argparse.ArgumentParser(description="Camera-only fetch agent for free vision models.")
    parser.add_argument("command", nargs="?")
    parser.add_argument("--listen", action="store_true", help="serve the Unity panel's AI COMMAND box")
    parser.add_argument("--port", type=int, help="engine port (default: find it on 8080 or 8081)")
    parser.add_argument("--fast", action="store_true", help="preset speed: the model only interprets the request, the engine's planner drives (demo mode, not camera-only)")
    parser.add_argument("--provider", choices=["claude", "gemini", "groq"])
    parser.add_argument("--model")
    parser.add_argument("--sheet", choices=["on", "off"], help="full-circle 8-view sheet for the search (default: on, except for small models)")
    args = parser.parse_args(argv)
    if not args.listen and not args.command:
        parser.error("give a command or --listen")
    client, model, _ = make_client(args.provider, args.model)
    scout = Scout(client, model, find_engine(args.port), use_sheet=None if args.sheet is None else args.sheet == "on")
    if args.fast:
        print("Fast mode: the model is told what is in the scene. Use it for demos, not judged runs.")
    if args.listen:
        listen(scout, fast=args.fast)
    else:
        print(json.dumps(run_fast(scout, args.command) if args.fast else scout.run(args.command), indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())

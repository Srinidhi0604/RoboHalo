"""Phase 2 POV clip builder.

The engine writes robot-camera frames (1280x720 JPEG, 30 per second of robot motion) while a run is
active. This turns them into the clip the crowd watches:
  - mp4 (H.264 where available), 1280x720, no audio
  - 10-20 s long: sped up if the motion took longer, last frame held if it was shorter
  - the typed command shown on screen for the first 2 seconds
  - nothing else drawn: no team name, logo or code

Usage: python -m agent.clip <frames_dir> "<command>" <out.mp4>
       python -m agent.clip --check <clip.mp4>     (exit 1 and list failed rules if invalid)
"""

import json
import sys
from pathlib import Path

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFont

FPS = 30
MIN_SECONDS, MAX_SECONDS = 10.0, 20.0
TARGET_MAX = 19.5   # stay safely under the 20 s rule
TARGET_MIN = 10.5   # and safely over the 10 s rule
OVERLAY_SECONDS = 2.0


def _font(size):
    for name in ("arialbd.ttf", "arial.ttf", "DejaVuSans-Bold.ttf"):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    return ImageFont.load_default()


def overlay_command(bgr, command):
    """Draw the typed command in a dark banner across the top of the frame."""
    image = Image.fromarray(cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)).convert("RGBA")
    layer = Image.new("RGBA", image.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(layer)
    font = _font(max(24, image.height // 18))
    text = "“" + command.strip() + "”"
    left, top, right, bottom = draw.textbbox((0, 0), text, font=font)
    w, h = right - left, bottom - top
    pad = h // 2 + 8
    x = (image.width - w) // 2
    y = image.height // 14
    draw.rounded_rectangle((x - pad, y - pad, x + w + pad, y + h + pad), radius=pad, fill=(0, 0, 0, 170))
    draw.text((x - left, y - top), text, font=font, fill=(255, 255, 255, 255))
    merged = Image.alpha_composite(image, layer).convert("RGB")
    return cv2.cvtColor(np.asarray(merged), cv2.COLOR_RGB2BGR)


def plan_timeline(n_frames, fps_in=FPS):
    """Map output frames to source frame indices so the clip lands in the 10-20 s window."""
    motion = n_frames / fps_in
    speed = max(1.0, motion / TARGET_MAX)          # too long: play faster
    played = int(round(motion / speed * FPS))
    indices = [min(int(k * speed * fps_in / FPS), n_frames - 1) for k in range(max(played, 1))]
    indices[-1] = n_frames - 1                     # always end on the final frame (object released)
    while len(indices) < int(TARGET_MIN * FPS):    # too short: hold the final frame
        indices.append(n_frames - 1)
    return indices, speed


def _writer(path, size):
    for codec in ("avc1", "mp4v"):  # H.264 via the OS encoder, else MPEG-4 Part 2
        writer = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*codec), FPS, size)
        if writer.isOpened():
            return writer, codec
    raise RuntimeError("no mp4 encoder available")


def build_clip(frames_dir, command, out_path):
    frames = sorted(Path(frames_dir).glob("frame_*.jpg"))
    if not frames:
        raise ValueError(f"no frames recorded in {frames_dir}")
    first = cv2.imread(str(frames[0]))
    height, width = first.shape[:2]
    indices, speed = plan_timeline(len(frames))
    writer, codec = _writer(out_path, (width, height))
    cache = {}
    try:
        for k, index in enumerate(indices):
            if index not in cache:
                cache.clear()
                cache[index] = cv2.imread(str(frames[index]))
            frame = cache[index]
            if k < OVERLAY_SECONDS * FPS:
                frame = overlay_command(frame, command)
            writer.write(frame)
    finally:
        writer.release()
    Path(out_path).with_suffix(".json").write_text(json.dumps(
        {"command": command, "overlay_seconds": OVERLAY_SECONDS, "source_frames": len(frames), "speedup": round(speed, 3)}), encoding="utf-8")
    info = validate_clip(out_path)
    info.update({"codec": codec, "speedup": round(speed, 3), "source_frames": len(frames),
                 "motion_seconds": round(len(frames) / FPS, 2)})
    return info


def validate_clip(path):
    """Enforce every clip rule from the brief and say which ones fail.

    Checked: .mp4 file, readable, every frame decodable, 720p or higher, 10-20 s, and the typed
    command overlay recorded for at least the first 2 s (from the sidecar the builder writes).
    """
    path = Path(path)
    problems = []
    if path.suffix.lower() != ".mp4":
        problems.append("file is not .mp4")
    if not path.exists():
        return {"path": str(path), "ok": False, "problems": problems + ["file does not exist"]}
    capture = cv2.VideoCapture(str(path))
    if not capture.isOpened():
        return {"path": str(path), "ok": False, "problems": problems + ["file cannot be opened as video"]}
    declared = int(capture.get(cv2.CAP_PROP_FRAME_COUNT))
    fps = capture.get(cv2.CAP_PROP_FPS) or FPS
    width, height = int(capture.get(cv2.CAP_PROP_FRAME_WIDTH)), int(capture.get(cv2.CAP_PROP_FRAME_HEIGHT))
    decoded = 0
    while capture.grab():
        decoded += 1
    capture.release()
    if decoded == 0:
        problems.append("no readable frames")
    elif abs(decoded - declared) > 2:
        problems.append(f"only {decoded} of {declared} frames are readable")
    duration = decoded / fps
    if not MIN_SECONDS <= duration <= MAX_SECONDS:
        problems.append(f"duration {duration:.1f}s outside 10-20s")
    if height < 720 or width < 1280:
        problems.append(f"{width}x{height} is below 1280x720")
    sidecar = path.with_suffix(".json")
    if not sidecar.exists():
        problems.append("overlay record missing (command not shown for the first 2 s)")
    else:
        meta = json.loads(sidecar.read_text(encoding="utf-8"))
        if not str(meta.get("command", "")).strip() or float(meta.get("overlay_seconds", 0)) < OVERLAY_SECONDS:
            problems.append("typed command not shown for the first 2 s")
    return {"path": str(path), "seconds": round(duration, 2), "width": width, "height": height, "ok": not problems, "problems": problems}


check_clip = validate_clip


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--check":
        report = validate_clip(sys.argv[2])
        print(json.dumps(report, indent=2))
        sys.exit(0 if report["ok"] else 1)
    if len(sys.argv) != 4:
        sys.exit(__doc__)
    print(build_clip(sys.argv[1], sys.argv[2], sys.argv[3]))

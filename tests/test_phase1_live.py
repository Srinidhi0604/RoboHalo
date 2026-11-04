"""Phase 1 acceptance tests against the LIVE Unity engine on :8080 (press Play first).

Run: python -m pytest tests/test_phase1_live.py -v -s
No model API is used; the Claude-image criterion is checked separately once the key is available.
"""

import json
import re
import struct
import time
from pathlib import Path
from urllib.error import HTTPError, URLError

import pytest

from agent.builtin import run as run_builtin
from agent.run_agent import SYSTEM, TOOLS, Engine

E = Engine()
ROOT = Path(__file__).resolve().parents[1]


def call(path, body=None):
    """Return (status, json_or_bytes); HTTP errors are returned, not raised."""
    try:
        return 200, E.call(path, body)
    except HTTPError as error:
        return error.code, json.loads(error.read())


def reset():
    E.call("/reset", {})


@pytest.fixture(autouse=True, scope="module")
def engine_up():
    try:
        E.call("/state")
    except URLError:
        pytest.skip("engine not running on :8080")


def jpeg_size(data):
    i = 2
    while i < len(data):
        marker = data[i + 1]
        if marker in (0xC0, 0xC2):
            h, w = struct.unpack(">HH", data[i + 5:i + 9])
            return w, h
        i += 2 + struct.unpack(">H", data[i + 2:i + 4])[0]


# ---------- data contract ----------

def test_state_has_no_object_data():
    reset()
    _, state = call("/state")
    assert {"name", "x", "y", "heading", "holding"} <= state.keys()
    assert state["name"] == "Idle" and state["holding"] is False
    assert not any(k in state for k in ("target", "objects", "color", "colour", "shape", "zone"))


def test_look_is_640x480_jpeg():
    _, frame = call("/look")
    assert frame[:2] == b"\xff\xd8" and jpeg_size(frame) == (640, 480)


def test_depth_is_float_or_null_and_level():
    reset()
    readings = []
    for _ in range(24):
        call("/turn", {"degrees": 15})
        readings.append(call("/depth")[1])
    assert all(r is None or 0 < r <= 10 for r in readings)
    assert any(r is None for r in readings) and any(r is not None for r in readings)  # open floor is not a reading


def test_move_and_turn_contract():
    reset()
    _, t = call("/turn", {"degrees": 90})
    assert abs(t["heading"] - 90) < 1
    _, m = call("/turn", {"degrees": -90})
    _, m = call("/move", {"distance": 0.5})
    assert m["collided"] is False and abs(m["moved"] - 0.5) < 0.05
    _, m = call("/move", {"distance": -0.5})
    assert m["moved"] < 0 and abs(m["moved"] + 0.5) < 0.05
    assert call("/move", {"distance": 99})[0] == 400


def test_pickup_out_of_reach_fails_without_leaking_identity():
    reset()
    _, r = call("/pickup", {})
    assert r["ok"] is False and set(r) <= {"success", "ok", "collided", "in_zone", "moved", "heading"}


def test_clock_is_frozen_when_idle():
    reset()
    a = call("/state")[1]
    time.sleep(1.5)
    assert call("/state")[1] == a


def test_unresolvable_command_is_accepted():
    reset()
    assert call("/start", {"command": "Bring me something to drink"})[0] == 200


# ---------- state machine & outcomes ----------

def test_three_different_commands_pass():
    reset()
    for command in ("Drop the blue box", "Bring the round red thing", "Bring the tallest object"):
        assert run_builtin(command)["outcome"] == "pass", command


def test_done_rejects_further_actions_with_409():
    assert call("/state")[1]["name"] == "Done"
    assert call("/move", {"distance": 0.1})[0] == 409
    assert call("/pickup", {})[0] == 409


def test_full_set_passes_without_reset_and_without_collision():
    reset()
    for command in ("Bring the blue cube", "Bring the red cube", "Bring the red sphere",
                    "Bring the yellow cone", "Bring the blue sphere", "Bring the yellow cylinder"):
        result = run_builtin(command)
        assert result["outcome"] == "pass", result
    assert call("/state")[1]["retries"] < 3


def test_fail_collision_after_three_blocked_moves():
    reset()
    call("/start", {"command": "Bring the blue cube"})
    call("/turn", {"degrees": 90})   # heading 90 faces the open room; keep looking for a wall
    wall_heading = None
    for _ in range(24):
        call("/turn", {"degrees": 15})
        d = call("/depth")[1]
        if d is not None and d < 1.5:
            wall_heading = True
            break
    assert wall_heading, "no near obstacle found within 1.5 m of start"
    last = None
    for _ in range(6):
        status, last = call("/move", {"distance": 2})
        if status == 409:
            break
    state = call("/state")[1]
    assert state["name"] == "Done" and state["outcome"] == "fail_collision", state
    assert call("/move", {"distance": 0.5})[0] == 409


def pick_up_with_builtin(command):
    """Let the engine's built-in runner drive to an object and stop once it is held."""
    E.call("/run_builtin", {"command": command, "pickup_only": True})
    for _ in range(240):
        time.sleep(0.5)
        if call("/state")[1]["holding"]:
            time.sleep(0.5)  # let the runner finish its coroutine
            return
    raise AssertionError("built-in pickup did not finish")


def test_fail_not_reached_when_dropped_outside_zone():
    reset()
    pick_up_with_builtin("Bring the red cube")
    _, dropped = call("/drop", {})
    assert dropped["in_zone"] is False
    assert call("/state")[1]["outcome"] == "fail_not_reached"


def test_fail_wrong_object():
    """The engine's planner carries the red cube into the zone, but the run's command was the cone."""
    reset()
    E.call("/run_builtin", {"command": "Bring the red cube", "score_as": "Bring the yellow cone"})
    for _ in range(360):
        time.sleep(0.5)
        state = call("/state")[1]
        if state["name"] == "Done":
            break
    assert state["outcome"] == "fail_wrong_object", state


# ---------- leak checks (static) ----------

def test_agent_prompt_and_tools_describe_no_scene():
    text = (SYSTEM + json.dumps(TOOLS)).lower()
    for word in ("red", "blue", "yellow", "green", "cube", "sphere", "cylinder", "cone", "pallet", "warehouse"):
        assert not re.search(rf"\b{word}\b", text), word


def test_frame_filenames_are_neutral():
    for line in (ROOT / "agent" / "run_agent.py").read_text().splitlines():
        if "frame-" in line:
            assert not re.search(r"red|blue|yellow|cube|sphere", line.lower())


# ---------- camera views (operator display) ----------

def screen():
    import io
    import numpy as np
    from PIL import Image
    return np.asarray(Image.open(io.BytesIO(E.call("/screen"))).convert("L"), dtype=float)


def test_camera_switch_changes_the_screen():
    names, shots = [], []
    for index in range(4):
        names.append(call("/view", {"index": index})[1]["view"])
        time.sleep(0.4)
        shots.append(screen())
    assert names == ["Overview", "Follow", "Top-down", "Robot POV"]
    for i in range(4):
        for j in range(i + 1, 4):
            assert abs(shots[i] - shots[j]).mean() > 3, f"views {names[i]} and {names[j]} look identical"
    call("/view", {"index": 0})


# ---------- Phase 2 clip ----------

def test_recorded_builtin_run_produces_a_valid_clip():
    reset()
    result = run_builtin("Bring the yellow cone", record=True)
    assert result["outcome"] == "pass"
    clip = result["clip"]
    assert clip["ok"], clip
    assert (clip["width"], clip["height"]) == (1280, 720)
    assert 10 <= clip["seconds"] <= 20
    assert call("/record")[1]["recording"] is False


# ---------- collision handling ----------

def test_turning_the_gripper_into_a_wall_is_a_collision():
    reset()
    call("/start", {"command": "Bring the yellow cone"})
    call("/turn", {"degrees": -90})          # heading 270: wall straight ahead of the start
    assert (call("/depth")[1] or 99) < 2, "expected a wall ahead of the start position"
    call("/turn", {"degrees": 50})           # angle towards the wall
    hit = False
    for _ in range(25):
        status, m = call("/move", {"distance": 0.1})
        if m.get("collided"):
            hit = True
            break
    assert hit, "never reached the wall"
    _, toward = call("/turn", {"degrees": -60})  # swing the gripper into the wall
    assert toward["collided"] is True
    _, away = call("/turn", {"degrees": 90})     # turning away from contact is always allowed
    assert away["collided"] is False
    assert call("/state")[1]["retries"] == 2

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

from agent.builtin import run as _run_builtin
from agent.run_agent import SYSTEM, TOOLS, Engine

E = Engine()  # finds the arena engine on 8080 or 8081


def run_builtin(command, **kwargs):
    return _run_builtin(command, engine=E, **kwargs)
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


# ---------- A2: clock, responsiveness, stretch obstacle ----------

@pytest.fixture(autouse=True, scope="module")
def real_time_speed():
    """Timing checks need 1x; restore whatever the operator had selected."""
    try:
        previous = E.call("/speed")["scale"]
    except Exception:
        yield
        return
    E.call("/speed", {"scale": 1.0})
    yield
    E.call("/speed", {"scale": previous})


def timed(path, body):
    start = time.time()
    call(path, body)
    return time.time() - start


def test_move_and_turn_run_at_the_documented_speeds():
    reset()
    call("/start", {"command": "Bring the blue cube"})
    move = timed("/move", {"distance": 1.0})
    assert 0.9 <= move <= 1.6, f"1 m move took {move:.2f}s (expected ~1 s at 1 m/s)"
    turn = timed("/turn", {"degrees": 90})
    assert 0.9 <= turn <= 1.6, f"90 deg turn took {turn:.2f}s (expected ~1 s at 90 deg/s)"


def test_reads_stay_responsive_during_a_move():
    import threading
    reset()
    call("/start", {"command": "Bring the blue cube"})
    started = time.time()
    mover = threading.Thread(target=lambda: call("/move", {"distance": 1.0}))
    mover.start()
    time.sleep(0.25)
    xs, latencies = [], []
    while mover.is_alive():
        t = time.time()
        xs.append(call("/state")[1]["x"])
        call("/depth")
        assert call("/look")[1][:2] == bytes([0xFF, 0xD8])
        latencies.append(time.time() - t)
        time.sleep(0.05)
    mover.join()
    assert len(xs) >= 2, "state calls were queued until the move finished"
    assert max(xs) > min(xs), "state did not show the robot moving"
    assert max(latencies) < 0.8, f"slow read during motion: {max(latencies):.2f}s"


def test_world_is_frozen_while_the_agent_waits():
    import hashlib
    reset()
    call("/start", {"command": "Bring the blue cube"})
    first = (call("/state")[1], hashlib.md5(call("/look")[1]).hexdigest())
    time.sleep(3.0)                                  # a slow Claude call
    second = (call("/state")[1], hashlib.md5(call("/look")[1]).hexdigest())
    assert first == second


def test_detour_wall_is_driven_around_not_through():
    reset()
    assert call("/obstacle", {"on": True})[1]["on"] is True
    try:
        for command in ("Bring the red cube", "Bring the yellow cylinder"):
            result = run_builtin(command)
            assert result["outcome"] == "pass", result
        assert call("/state")[1]["retries"] < 3
    finally:
        call("/obstacle", {"on": False})
    assert call("/obstacle")[1]["on"] is False


# ---------- dark mode (demo only) ----------

def look_array():
    import io
    import numpy as np
    from PIL import Image
    return np.asarray(Image.open(io.BytesIO(E.call("/look"))).convert("RGB"), dtype=int)


def test_dark_mode_gives_monochrome_frames_and_restores_colour():
    reset()
    call("/turn", {"degrees": 40})                 # face the coloured objects
    lit = look_array()
    spread = lambda a: a.max(axis=2) - a.min(axis=2)
    assert spread(lit).max() > 60, "lit scene should have colour"
    try:
        assert call("/dark", {"on": True})[1]["on"] is True
        for _ in range(10):                        # lighting can take a few frames to settle
            time.sleep(0.3)
            dark = look_array()
            if dark[180:300, 260:380].mean() > dark[:40].mean():
                break
        assert spread(dark).max() <= 6, "IR frame must be grey"  # JPEG chroma noise only
        assert dark.mean() < lit.mean(), "room should be darker with the lights off"
        assert dark[180:300, 260:380].mean() > dark[:40].mean(), "illuminator should light the area ahead"
        result = run_builtin("Bring the tallest object", record=True)
        assert result["outcome"] == "pass"
        assert result["clip"]["ok"], result["clip"]
    finally:
        call("/dark", {"on": False})
    time.sleep(0.3)
    reset()
    call("/turn", {"degrees": 40})
    assert spread(look_array()).max() > 60, "colour should return with the lights"


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


# ---------- /go_to (engine path-finding) and seeded layouts ----------

def test_go_to_routes_around_the_wall_and_honours_stop_short():
    reset()
    call("/start", {"command": "Bring the blue cube"})
    start = call("/state")[1]
    target = (start["x"], start["y"] - 4.0)          # straight through where the detour wall stands
    call("/obstacle", {"on": True})
    try:
        status, result = call("/go_to", {"x": target[0], "y": target[1]})
        assert status == 200 and result["reached"] is True and result["collided"] is False, result
        state = call("/state")[1]
        assert abs(state["x"] - target[0]) < 0.4 and abs(state["y"] - target[1]) < 0.4, state
        assert state["retries"] == 0
        _, back = call("/go_to", {"x": start["x"], "y": start["y"], "stop_short": 1.0})
        after = call("/state")[1]
        gap = ((after["x"] - start["x"]) ** 2 + (after["y"] - start["y"]) ** 2) ** 0.5
        assert back["reached"] is True and 0.7 < gap < 1.4, (back, gap)
    finally:
        call("/obstacle", {"on": False})
    assert call("/go_to", {"x": 999, "y": 0})[0] == 400


def test_seeded_shuffle_is_repeatable_and_still_deliverable():
    def view(seed):
        E.call("/reset", {"seed": seed})
        call("/turn", {"degrees": 40})
        return look_array()

    def changed(a, b):
        return float((abs(a - b).max(axis=2) > 40).mean())

    default, first, other, again = view(0), view(7), view(8), view(7)
    assert changed(first, again) < 0.0005, "same seed must give the same layout"
    assert changed(first, default) > 0.002, "seed 7 should differ from the default layout"
    assert changed(first, other) > 0.002, "different seeds should differ"
    E.call("/reset", {"seed": 7})
    for command in ("Bring the red cube", "Bring the blue sphere"):
        assert run_builtin(command)["outcome"] == "pass", command
    assert changed(view(0), default) < 0.0005, "a plain reset restores the default layout"


# ---------- collisions: the planner should avoid every obstacle ----------

def test_no_collisions_across_shuffled_layouts():
    """All six deliveries on three layouts, with and without the detour wall: zero bumps."""
    bumps = []
    for seed, wall in ((0, False), (7, False), (21, True)):
        E.call("/reset", {"seed": seed})
        call("/obstacle", {"on": wall})
        try:
            for name in ("red sphere", "red cube", "blue sphere", "blue cube", "yellow cylinder", "yellow cone"):
                result = run_builtin("Bring the " + name)
                retries = call("/state")[1]["retries"]
                assert result["outcome"] == "pass", (seed, name, result)
                if retries:
                    bumps.append((seed, wall, name, retries))
        finally:
            call("/obstacle", {"on": False})
    assert not bumps, f"collisions (seed, wall, item, count): {bumps}"


# ---------- extra walls: collision handling ----------
# /obstacle {"on": true, "walls": 4} stands four walls around the robot's start: one straight ahead in each
# of these headings, with open corners between them. {"on": true} alone is still the single detour wall.

WALL_HEADINGS = (0, 90, 180, 270)


def set_walls(count):
    return call("/obstacle", {"on": count > 0, "walls": count})[1]


def face(heading):
    reset()
    call("/start", {"command": "Bring the blue cube"})
    if heading:
        call("/turn", {"degrees": heading if heading <= 180 else heading - 360})


def test_wall_count_can_be_set_and_plain_on_is_still_one_wall():
    try:
        assert set_walls(4)["walls"] == 4
        assert call("/obstacle")[1]["on"] is True and call("/obstacle")[1]["walls"] == 4
        assert call("/obstacle", {"on": True})[1]["walls"] == 1
    finally:
        assert set_walls(0)["on"] is False


def test_each_wall_shows_in_depth_and_stops_the_robot():
    open_depth = {}
    for heading in WALL_HEADINGS:
        face(heading)
        open_depth[heading] = call("/depth")[1]
    set_walls(4)
    try:
        for heading in WALL_HEADINGS:
            face(heading)
            depth = call("/depth")[1]
            assert depth is not None and depth < 2.4, (heading, depth)            # the wall is nearer than open floor (about 2.9)
            assert open_depth[heading] is None or depth <= open_depth[heading] + 0.01, (heading, depth, open_depth[heading])
            travelled, hit = 0.0, None
            for _ in range(3):
                _, hit = call("/move", {"distance": 1.0})
                travelled += hit["moved"]
                if hit["collided"]:
                    break
            assert hit["collided"] is True, (heading, hit)                        # stopped by the wall, not driven through
            assert travelled < 2.3, (heading, travelled)
            state = call("/state")[1]
            assert state["name"] == "Blocked" and state["retries"] == 1, (heading, state)
            assert call("/move", {"distance": -0.4})[1]["collided"] is False     # it can back away again
    finally:
        set_walls(0)


def test_scripted_fetch_and_go_to_get_past_four_walls():
    reset()
    set_walls(4)
    try:
        for command in ("Bring the red cube", "Bring the blue sphere"):
            result = run_builtin(command)
            assert result["outcome"] == "pass", result
            assert call("/state")[1]["retries"] < 3
        reset()
        call("/start", {"command": "Bring the blue cube"})
        start = call("/state")[1]
        status, result = call("/go_to", {"x": start["x"] + 3.0, "y": start["y"]})   # straight through the east wall
        assert status == 200 and result["reached"] is True and result["collided"] is False, result
    finally:
        set_walls(0)

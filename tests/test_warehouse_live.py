"""Live checks for the warehouse scene (engine/Assets/Scenes/Warehouse.unity in Play mode, port 8080).

Skipped when the engine on :8080 is not the warehouse engine. No model is called.
Run: python -m pytest tests/test_warehouse_live.py -v
"""

import hashlib
import json
import struct
import time
from urllib.error import HTTPError, URLError

import pytest

from agent.run_agent import Engine

E = Engine("http://127.0.0.1:8080")  # the warehouse engine always uses 8080
STATE_KEYS = {"name", "state", "outcome", "x", "y", "heading", "holding", "retry_count", "retries"}
ACTION_KEYS = {"success", "collided", "in_zone", "ok", "moved", "heading"}


def call(path, body=None):
    try:
        return 200, E.call(path, body)
    except HTTPError as error:
        return error.code, json.loads(error.read())


@pytest.fixture(autouse=True, scope="module")
def warehouse_engine():
    try:
        E.call("/state")
    except URLError:
        pytest.skip("no engine on :8080")
    if call("/view")[0] != 404:
        pytest.skip("the engine on :8080 is the arena engine, not the warehouse")


def reset():
    assert call("/reset", {})[0] == 200


def jpeg_size(data):
    i = 2
    while i < len(data):
        if data[i + 1] in (0xC0, 0xC2):
            h, w = struct.unpack(">HH", data[i + 5:i + 9])
            return w, h
        i += 2 + struct.unpack(">H", data[i + 2:i + 4])[0]


def face_the_stacks_and_pick_up():
    """From home (0, 0) facing +x: turn to +z and stop 1.25 m short of the nearest aisle stack."""
    call("/turn", {"degrees": -90})
    for distance in (1.0, 1.0, 0.5):
        assert call("/move", {"distance": distance})[1]["collided"] is False
    return call("/pickup", {})[1]


def carry_to_the_drop_zone():
    call("/turn", {"degrees": 180})
    for _ in range(4):
        assert call("/move", {"distance": 1.0})[1]["collided"] is False
    return call("/drop", {})[1]


def test_state_reply_has_robot_data_only():
    reset()
    _, state = call("/state")
    assert set(state) == STATE_KEYS
    assert state["name"] == "Idle" and state["holding"] is False and state["retries"] == 0


def test_look_is_640x480_jpeg():
    _, frame = call("/look")
    assert frame[:2] == bytes([0xFF, 0xD8]) and jpeg_size(frame) == (640, 480)


def test_any_command_is_accepted_without_revealing_what_the_engine_knows():
    for command in ("Bring me something to drink", "Fetch the purple hexagon", "Bring three red crates"):
        reset()
        status, reply = call("/start", {"command": command})
        assert status == 200 and set(reply) == ACTION_KEYS, (command, reply)
        assert call("/state")[1]["name"] == "Locate"


def test_move_and_turn_speeds_and_frozen_clock():
    reset()
    call("/start", {"command": "Bring a box"})
    started = time.time()
    _, moved = call("/move", {"distance": 1.0})
    assert abs(moved["moved"] - 1.0) < 0.05 and 0.8 <= time.time() - started <= 2.0
    started = time.time()
    _, turned = call("/turn", {"degrees": 90})
    assert abs(turned["heading"] - 90) < 1 and 0.8 <= time.time() - started <= 2.0
    before = (call("/state")[1], hashlib.md5(call("/look")[1]).hexdigest())
    time.sleep(2.5)   # the agent is "thinking": nothing may move, not even the other robots or the belt
    assert (call("/state")[1], hashlib.md5(call("/look")[1]).hexdigest()) == before


def test_pickup_out_of_reach_fails_and_says_nothing_else():
    reset()
    call("/start", {"command": "Bring a box"})
    _, reply = call("/pickup", {})
    assert reply["ok"] is False and set(reply) == ACTION_KEYS
    assert call("/drop", {})[1]["ok"] is False


def test_two_item_command_runs_until_both_are_delivered():
    reset()
    call("/start", {"command": "Bring 2 boxes"})
    assert face_the_stacks_and_pick_up()["ok"] is True
    assert call("/state")[1]["holding"] is True
    assert carry_to_the_drop_zone()["in_zone"] is True
    assert call("/state")[1]["name"] == "Locate"          # one of two delivered: the run goes on
    call("/turn", {"degrees": 180})
    for _ in range(4):
        call("/move", {"distance": 1.0})
    assert call("/pickup", {})[1]["ok"] is True
    assert carry_to_the_drop_zone()["in_zone"] is True
    state = call("/state")[1]
    assert (state["name"], state["outcome"]) == ("Done", "pass")
    assert call("/move", {"distance": 0.5})[0] == 409


def test_drop_outside_the_zone_is_fail_not_reached():
    reset()
    call("/start", {"command": "Bring a box"})
    assert face_the_stacks_and_pick_up()["ok"] is True
    _, dropped = call("/drop", {})
    assert dropped["in_zone"] is False
    state = call("/state")[1]
    assert (state["name"], state["outcome"]) == ("Done", "fail_not_reached")


def test_three_collisions_end_the_run():
    reset()
    call("/start", {"command": "Bring a box"})
    call("/turn", {"degrees": -90})
    collisions = 0
    for _ in range(12):
        status, reply = call("/move", {"distance": 1.0})
        if status == 409:
            break
        collisions += bool(reply["collided"])
    state = call("/state")[1]
    assert collisions == 3 and (state["name"], state["outcome"]) == ("Done", "fail_collision")
    reset()

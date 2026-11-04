"""Run while the Unity scene or player is active: python tests/smoke_engine.py."""

import json
from urllib.request import Request, urlopen

BASE = "http://localhost:8080"


def request(path, body=None):
    data = None if body is None else json.dumps(body).encode()
    req = Request(BASE + path, data=data, headers={"Content-Type": "application/json"})
    with urlopen(req, timeout=10) as response:
        return response.read(), response.headers.get_content_type()


state, content_type = request("/state")
assert content_type == "application/json"
state = json.loads(state)
assert {"state", "x", "y", "heading", "holding", "retry_count"} <= state.keys()
assert not ({"objects", "object_ids", "object_positions", "drop_zone"} & state.keys())

image, content_type = request("/look")
assert content_type == "image/jpeg" and image[:2] == b"\xff\xd8"
assert image[-2:] == b"\xff\xd9"

turn, _ = request("/turn", {"degrees": 15})
assert json.loads(turn)["success"] is True
move, _ = request("/move", {"distance": 0.1})
assert "collided" in json.loads(move)
print("Engine API smoke check passed")

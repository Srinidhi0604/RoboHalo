# RoboHalo Phase 1 Build Specification

Status: implemented baseline | Scope: hackathon Phase 1 only

## 1. Goal and acceptance result

Given one plain-language command, the robot must use only its forward camera and optional single-ray depth reading to find the requested object, drive to it, snap-pick it up, carry it to the marked zone, and release it. A run ends with exactly one of:

- `pass`: the requested object was released in the zone.
- `fail_wrong_object`: another object was released in the zone.
- `fail_not_reached`: the agent stopped, exceeded 60 decisions, or dropped outside the zone.
- `fail_collision`: the engine recorded three blocked movement attempts.

## 2. Processes and ownership

| Process | Responsibility |
| --- | --- |
| Unity engine | Run the scene at 30 FPS or more, render the robot camera, expose `localhost:8080`, execute physics and collisions, hold the state machine, and privately score the final object. |
| Python agent | Accept one command, start the run, request camera/depth/state, call Claude once per decision, validate one tool call, execute it, enforce 60 steps, and write frames plus JSONL. |
| Claude API | Receive the command, latest allowed frame, optional depth, permitted state, and compact action history; return exactly one tool call. |

The engine receives the command through `POST /start` only so it can privately score `pass` versus `fail_wrong_object`. It never returns the target, scene object table, colors, shapes, object positions, counts, IDs, or zone coordinates.

## 3. Required scene

- Flat 6 m × 6 m room, floor, four walls, no furniture.
- Six primitive objects covering every allowed Round 1 type: red sphere, red cube, blue sphere, blue cube, yellow cylinder, and yellow cone. Colors and shapes intentionally repeat. The yellow cylinder is tallest.
- Green 1 m × 1 m drop zone.
- TurtleBot3 Burger visual with a capsule physics collider.
- Forward camera at 0.5 m with 640×480 JPEG output and 90° horizontal field of view.
- Directional light and a separate overview camera for demonstration.

## 4. State machine

| State | Entry and exit rule |
| --- | --- |
| `Idle` | Wait for a valid `POST /start` command. |
| `Locate` | Claude inspects the current robot image and selects a motion or pickup tool. |
| `Go to object` | Engine moves without an attached object. |
| `Pick up` | `/pickup` searches within 0.5 m and ±30°, then snaps the nearest eligible object to the robot. |
| `Carry` | Engine moves or turns with the object attached. |
| `Drop` | `/drop` releases the object and privately evaluates target and zone. |
| `Blocked` | Collision stops movement, backs off 0.1 m, and increments retries. The agent must turn and retry. |
| `Done` | Terminal outcome is available in `/state`; further action requests return HTTP 409. |

Three collision retries produce `fail_collision`. The synchronous movement API makes lack of progress observable as a collision or action failure; the 60-step agent limit handles repeated non-progress.

## 5. Engine API

All responses except `/look` are JSON. Motion units are metres and clockwise degrees.

| Endpoint | Contract |
| --- | --- |
| `POST /start {"command":"…"}` | Validate one command and enter `Locate`. Target resolution stays private. |
| `GET /state` | `name`, robot `x`/`y`, `heading`, `holding`, `retries`, and terminal `outcome`; compatibility aliases `state` and `retry_count` are also present. No object metadata. |
| `GET /look` | Current 640×480 robot-camera JPEG. |
| `GET /depth` | Forward ray distance up to 10 m or JSON `null`. |
| `POST /move {"distance":n}` | Range -1 to 1 for the agent; engine rejects magnitudes over 3. Moves at 1 m/s and returns `moved` and `collided`. |
| `POST /turn {"degrees":n}` | Range -180 to 180. Turns at 90 degrees/s and returns `heading`. |
| `POST /pickup {}` | Returns `ok`; does not reveal identity. |
| `POST /drop {}` | Returns `ok` and `in_zone`; terminal identity appears only as the outcome category. |

## 6. Claude decision contract

Allowed tools are `move`, `turn`, `pickup`, `drop`, and terminal `fail`. Each response must contain exactly one `tool_use` block and no text. The agent rejects unknown tools, multiple content blocks, malformed arguments, moves outside ±1 m, and turns outside ±180°.

The request contains image bytes, the original command, optional single-ray depth, `state`/`holding`/`retry_count`, and up to six recent action results. It excludes robot coordinates from Claude context even though `/state` permits them. Scene metadata must never appear in the prompt or tool results.

The agent fetches the initial image and a new image after movement or rotation. Only the latest frame is included. Frames are stored locally for audit and are never converted into privileged text labels.

## 7. Run artifacts

Each run creates `runs/<timestamp-id>/` containing JPEG frames and `run.jsonl`. Every decision record contains `step`, UTC `timestamp`, `tool`, `args`, `result`, and `frame_path`. The final line contains command, outcome, step count, collision retry count, model, token count, and a nullable cost estimate. No API key is logged.

## 8. Run instructions

1. Open `engine/` with Unity `6000.6.3f1`, open `Assets/Scenes/RoboHalo.unity`, and press Play.
2. Install the agent once: `python -m pip install -r requirements.txt`.
3. Set `ANTHROPIC_API_KEY` and optionally `CLAUDE_MODEL`.
4. Run: `python -m agent.run_agent "Bring the round red thing"`.

Before using an AI key, run the same engine through the deliberately hardcoded
dummy planner: `python -m agent.manual_agent "Bring the round red thing"`.
This runner knows coordinates and exists only to verify HTTP, motion, collision,
pickup, carry, drop, camera, state, outcome, and logging. It must not be used in
a judged run or presented as image-driven behavior.

## 9. Verification matrix

| Requirement | Verification |
| --- | --- |
| Python decision validation, image payload, metadata boundary, frame and JSONL output | `python -m unittest discover -s tests -p "test_*.py" -v` |
| Unity compiles and the scene/player starts | Unity batch build or editor Play mode without compiler errors |
| Engine HTTP schema, JPEG, start, turn, and move | With Unity running: `python tests/smoke_engine.py` |
| 30 FPS, collision visibility, pickup/carry/drop, and all four real outcomes | Record and inspect live acceptance runs; these depend on rendered physics and a Claude API key. |

## 10. Phase 1 completion gate

Phase 1 is accepted after the unit suite, Unity compile, engine smoke check, and at least three unattended camera-driven commands finish successfully. One recorded collision run must visibly stop and recover. Logs must show no privileged object data in Claude requests.

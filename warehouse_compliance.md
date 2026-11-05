# Warehouse scene — compliance with the hackathon rules

Scene: `engine/Assets/Scenes/Warehouse.unity` (scripts `WarehouseDemo.cs`, `WarehouseApi.cs`, `WarehousePriority.cs`).
Checked against `spec_problem.md` and the rules page (endpoints, prohibited object data, optional sensors, control loop).

Status key: **OK** meets the rule, **Fixed** changed in this pass, **Deviation** differs from the brief and should be disclosed, **Gap** not built.

## 1. Endpoints

| Endpoint | Rule | Status | Notes |
| --- | --- | --- | --- |
| `GET /state` | State name, x/y, heading, holding, retry count. No object information. | OK | Reply fields: `name, state, outcome, x, y, heading, holding, retry_count, retries`. |
| `GET /look` | JPEG from the robot camera; the only view of objects. | OK | 640×480, 90° horizontal FOV, 0.5 m high at the robot front, tilted 10° down. |
| `GET /depth` | Single-ray distance straight ahead; does not identify the thing. | OK | One ray along the camera centre line, max 10 m. Because the camera is tilted down, open floor reads about 2.9 m. |
| `POST /move` | Moves; stops early on collision; returns collision status. | OK | 1 m/s. Backs off 0.1 m, state `Blocked`, third collision ends the run with `fail_collision`. |
| `POST /turn` | Turns in place. | OK | 90°/s. |
| `POST /pickup` | Attaches nearest object in reach; success/failure only. | OK | Top item of the nearest aisle stack within 1.6 m and ±30°. Never says which. |
| `POST /drop` | Releases; reports whether it landed in the zone. | OK | Sets the item down 1.25 m ahead. The zone is the moving belt. |
| `POST /start` | Takes the command. | Fixed | It used to answer 400 "does not identify a supported target" for commands it could not parse, which revealed what the engine recognises. Every command is now accepted. |
| `POST /reset` | — | OK | Robot home, pallets restocked. |

## 2. Prohibited object data

| Check | Status | Notes |
| --- | --- | --- |
| No names, IDs, colours, shapes, positions, counts in `/state` or action replies | OK | Verified from the reply types in the source (`tests/test_warehouse.py`). |
| No object data in error messages | Fixed | The only messages are: command required, run already started, run finished, invalid distance, invalid degrees, unknown endpoint, engine error. |
| No object data in the system prompt or tool descriptions | OK | The lift-robot prompt describes the robot, its sensors and its tools only. An automated test scans it for colour, shape and scene words. |
| No object data in file names | OK | Frames are `frame-001.png` and so on. |
| Scoring target stays inside the engine | OK | The engine parses colour, shape and count from the command for scoring only. Nothing about it is returned. |
| Box codes and QR patterns printed on boxes | OK | They are pixels in the camera image, like labels on real boxes. They are never sent as text. |
| Operator screen shows box codes and kinds | OK | The on-screen panels are for the operator. The agent never receives them. |

## 3. Camera as the only source: the free agent

- The free agent is the same loop as the Claude agent (`agent/run_agent.py`), with Groq's `qwen/qwen3.8-27b` behind it.
- Each step it sends: the command, one `/look` frame, the depth reading, the robot's own position and heading, holding, collision count, and one line per earlier step.
- An automated test builds the exact Groq request and checks that it contains one image and no colour, shape or scene words.
- **Quick mode (`agent/quick.py`) is not compliant and does not work with this scene.** It tells the model what is in the scene. Use it only for demos of the other (TurtleBot) scene.

## 4. Control loop

| Rule | Status | Notes |
| --- | --- | --- |
| One model call per step, one tool call out | OK | Agent side. |
| Engine clock frozen while the model thinks | OK | Under API control `Time.timeScale` is 0 unless an action is running, so the belt and the other robots stop too. |
| `/look` sent as an image block, not a description | OK | Agent side. |
| One `/look` after each move or turn; never two in a row | OK | Agent side. |
| JSONL log per step, frames saved as PNG | OK | Agent side: `runs/<run>/run.jsonl`, `frame-*.png`, `replay.html`. |
| Hard stop at 60 steps | OK | Agent side. Groq test runs stop at 25 by default. |

## 5. Scene

| Rule | Status | Notes |
| --- | --- | --- |
| Shapes in different colours; at least two share a colour and two share a shape | Fixed | Three shapes (cube crate, low flat carton, tall carton) in four colours (red, blue, yellow, brown). All 12 combinations are stocked in the aisle-facing stacks. |
| One room with four walls | Fixed | Four walls now enclose the floor. The robot camera sees them; the operator's overview hides them so they do not block the view. |
| One marked drop zone | Fixed | The drop zone is the moving belt; its side rails are now green. |
| One wheeled robot with a forward camera | OK | "Selector 1" (orange) is the API robot. |
| 4 to 6 primitive shapes on the floor | **Deviation** | There are 14 reachable stacks and many more boxes behind them, on pallets. Disclose it, or ask a mentor. |
| No furniture | **Deviation** | Pallets, belts, charging docks and three other robots are in the room. The brief counts obstacles as a stretch goal. Disclose it. |
| Pick-up is a snap on touch | **Deviation** | The robot lifts the top box of a stack from up to 1.6 m away instead of touching it. Tuned value; it is listed in the README table. |

## 6. Commands

- **Operator order box (autonomous mode):** understands colour, shape and count, for example `3 red crates`, `2 tall blue boxes`, `yellow flat carton`, `bring three crates`.
- **Agent runs (API mode):** any sentence is accepted. A command for several items keeps the run open until the last one is delivered. A wrong or misplaced delivery ends it.
- **Commands the engine cannot score** (for example ones that need reasoning it has no rule for) are scored leniently: any box in the zone counts as a pass.

## 7. Gaps

| Item | Status |
| --- | --- |
| Phase 2 POV clip for this scene | **Gap.** The clip recorder exists only in the TurtleBot scene's engine. |
| `/go_to` | Not present in this engine. The agent detects that and drives with move and turn only. |
| Turn collisions | Not checked in this engine; only moves stop on contact. |
| Live verification | The source checks pass. The live checks (`tests/test_warehouse_live.py`) still need a run in Unity. |

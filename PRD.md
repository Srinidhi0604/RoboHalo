# RoboHalo — Membrane Robot Hackathon PRD

Status: full product draft for team review | Source: `membrane_only_track_brief.pdf` | Event window: 24 hours

## 1. Goal

Build a simulated robot that accepts one plain-language fetch command and, without human input during the run, uses Claude's view of its forward camera to find the requested object, drive to it, pick it up, and deliver it to a marked drop zone. Produce evidence that the object choice came from camera pixels. After mentor sign-off, submit three robot POV clips to the human-feedback API.

## 2. Success criteria and gates

| Gate | Acceptance criterion | Target |
| --- | --- | --- |
| Scene and API | Room, 4–6 objects, zone, robot and all required endpoints work | Early Phase 1 |
| Autonomous fetch | At least three different commands end in `Done` with `pass`; no keyboard/mouse input during a run | By hour 14 |
| Image evidence | Logs show camera frames, Claude tool choices, collision recovery, and no privileged object data sent to Claude | Phase 1 sign-off |
| Mentor sign-off | Live demonstration accepted; Phase 2 credentials issued | Before Phase 2 |
| Hidden runs | Run each of three unseen commands exactly once | Hour 18 |
| Clip submission | Upload three compliant clips via feedback API | By hour 20 |
| Final submission | Public code repository, three hidden-run logs, image-reasoning paragraph, booked live demo | Event deadline |

Judging weights: fetch success/directness 40%, image-driven reasoning 25%, human verdict 20%, one working stretch goal 15%. Object-data leakage to Claude zeroes both Phase 1 components.

## 3. Users and workflow

- **Operator:** starts the engine and Python agent, types one command, and observes a hands-off run. May reset between runs.
- **Mentor:** sees three successful commands and inspects image/tool logs before unlocking Phase 2.
- **Judge:** supplies two commands, checks success and travel directness, and reads evidence logs.
- **Feedback participant:** watches a 10–20 second POV clip and answers the two fixed pulse questions.

Run flow: `Idle → Locate → Go to object → Pick up → Carry → Drop → Done`. Collision or no motion for two seconds enters `Blocked`; back off, turn and retry up to three times. Terminal outcomes: `pass`, `fail_wrong_object`, `fail_not_reached`, `fail_collision`. The engine may determine the true outcome internally, but must never reveal object identity to the agent or Claude.

## 4. Phase 1 functional requirements

### Scene and motion

- One flat room with four walls and no furniture for the baseline.
- Four to six floor objects using primitive cubes, spheres, cylinders or cones, in different colors. At least two share a color and at least two share a shape. Include a marked drop zone and wheeled robot; a capsule with a camera is sufficient.
- Render at least 30 FPS. Robot camera: 640×480 JPEG, 90° horizontal field of view, mounted at robot front 0.5 m above floor, tilted 10° down.
- Units are metres and degrees. Heading 0 points along +x; positive rotation is clockwise from above. Forward speed 1 m/s; turning speed 90°/s.
- Movement stops on collision, backs off 0.1 m, reports `collided=true`, and enters `Blocked`. Pickup snaps the nearest eligible object within 0.5 m and ±30° of heading. Carrying moves the attached object with the robot; dropping checks whether it landed in the zone.
- Freeze simulated time while Claude thinks. Advance scene only during `/move` and `/turn`; keep rendering responsive for the demo and recording.

### Local engine API (`localhost:8080`)

| Endpoint | Response contract |
| --- | --- |
| `GET /state` | State, robot x/y, heading, holding boolean, retry count. No object information. |
| `GET /look` | Current camera JPEG only. |
| `GET /depth` | Optional single forward-ray distance in metres (max 10), or null. No identity. |
| `POST /move {distance}` | Move forward/reverse and report collision status. |
| `POST /turn {degrees}` | Turn in place. |
| `POST /pickup` | Success/failure only; no identity. |
| `POST /drop` | Whether held object landed in zone. |

The engine keeps its scene object table private. Agent-facing responses, prompts, tool descriptions, file names and Claude context must contain no object names, IDs, colors, shapes, positions, counts, segmentation masks, named-target distances or drop-zone coordinates. Robot pose in `/state` is explicitly permitted. A private engine-side result may classify `fail_wrong_object` for scoring without exposing which object was held.

### Python agent and Claude

- Accept one typed command per run. Call the local engine API through a tool-use loop; Claude returns exactly one tool call per decision step.
- Supply the latest `/look` as an image content block, plus the command, optional single-ray depth, collision result and compact recent tool history. Do not replace the image with a text description.
- Take one new `/look` after each `/move` or `/turn`. Do not send consecutive images without an intervening move or turn. Keep earlier frames out of context.
- Enforce a 60-step hard stop and track API usage. Aim for about 25 steps; expected range is 15–40. Test HTTP plumbing with a dummy agent or lower-cost model before vision runs.
- Save one JSONL record per step with `step`, `timestamp`, `tool`, `args`, `result`, and `frame_path`. Save corresponding frames as PNG beside the log. Include a run summary with command, outcome, step count, retries and cost estimate. Sanitize any log shared with Claude according to the object-data boundary.
- Handle API failures and malformed tool calls with a bounded retry or terminal failure; never allow an unattended infinite loop.

## 5. Phase 2 requirements

Begins only after mentor sign-off and receipt of the feedback API key/guide. Record the robot's POV from leaving `Idle` through reaching `Done`. Export MP4 at 720p or higher, 10–20 seconds, with the typed command overlaid for the first two seconds. No team name, logo or code may be visible. At hour 18, run each hidden command once; upload all three clips by hour 20. The human-feedback API integration must follow the organizer's guide when issued. Each clip receives at least 30 responses: correct object delivered (yes/no) and a fault label (hit something, wrong object, did not reach zone, too slow, nothing wrong). Missing or noncompliant clips score zero for Phase 2.

## 6. Stretch goal selection

Choose one only after the baseline passes. Recommended: **Claude plans the full path using `/move` and `/turn` only**, since the baseline already requires those tools and no `go_to` shortcut is needed. Other options: add an obstacle blocking the straight path; demonstrate a behavior improvement from pulse results with before/after clips; or handle multi-view relational commands such as farthest cube from a green square.

## 7. Architecture and stack options

The brief requires an engine process and a separate Python agent. It does not mandate a specific game engine. These are implementation options, not organizer rules.

| Engine | Suggested stack | Strengths for this brief | Main cost/risk | Fit |
| --- | --- | --- | --- | --- |
| **Godot 4** | GDScript scene/API bridge + Python `anthropic` agent | Light project, fast primitive scene building, built-in camera/physics | HTTP server must be implemented using TCP networking or a local extension | Good fallback |
| **Unity 6** | C# scene/API + Python `anthropic` agent | Fast primitive scene setup, mature physics, camera render target and JPEG path | Local HTTP bridge and render-texture readback need implementation | **Recommended for this 24-hour build** |
| **Unreal Engine 5** | Blueprint/C++ scene/API + Python `anthropic` agent | Strong rendering and physics; Scene Capture 2D supports render targets | Larger project and build footprint; image extraction and custom endpoint integration may consume more time | Choose if team is already fluent in Unreal |
| **Panda3D** | Python engine + Python `anthropic` agent | One-language stack; documented offscreen render-to-texture; straightforward Python HTTP integration | More manual scene/physics/UI work; team must prove 30 FPS and collision behavior | Best if team is Python-first |

**Implemented engine:** Unity `6000.6.3f1`, the editor available on the build machine, using the **3D Built-In Render Pipeline** and C#. Earlier planning recommended Unity 6.3 LTS; the committed project should be opened with its actual version in `engine/ProjectSettings/ProjectVersion.txt` to avoid a risky downgrade. The planned full product adds FastAPI/Python for orchestration and the local product API, Python Anthropic SDK for the agent, SQLite and local files for run artifacts, React/Vite/TypeScript for the operator console, and FFmpeg for clips. The target laptop has only 6 GB system RAM, below Unity's 8 GB Editor recommendation. Keep the scene primitive, disable expensive post-processing, close other applications and build a standalone player for the demo; upgrade to 16 GB system RAM if possible. The Unity process owns its required `localhost:8080` API. Pin the exact Claude model ID supplied by organizers; the brief describes Sonnet 5.5, but the assigned key and model availability must be checked at the event.

### Component boundaries

```text
Operator console → FastAPI orchestrator → Python agent → Claude
       ↑                    │                  │           ↑ latest JPEG only
       │                    │                  ↓           │
       └── run events/status│            localhost:8080 ← Unity engine
                            │                  │
                            └→ SQLite + JSONL + PNG frames → FFmpeg → feedback API
```

Implementation detail to decide before coding: the engine may keep an internal evaluator for target correctness, but its result must be withheld until the run ends. The agent should receive only the permitted `/state`, camera, optional depth and action results. The operator UI may display private scoring only after a run ends and must never feed it back into Claude context.

### Claude integration point and decision contract

Integrate Claude in the **Python agent worker inside the FastAPI backend**. The frontend sends only the typed command to the backend. The Unity engine serves camera JPEGs and executes motion; it does not hold the Anthropic key or call Claude. The Python worker fetches `/look`, encodes that JPEG as an image content block, adds the command and permitted state/history, then calls Claude's Messages API through the Python SDK. It validates Claude's single `tool_use` block and maps it to one engine endpoint. This keeps API credentials, logging, retry limits and the object-data boundary in one place.

Per-step loop: `GET /look` → optional `GET /depth` and permitted `GET /state` → Claude decision → validate exactly one of `move`, `turn`, `pickup`, `drop` (or a terminal fail action) → call engine → append JSONL and save PNG → repeat after movement/turn. A fixed system prompt defines units, camera orientation, state rules, tool schemas and the prohibition on privileged object data. Claude is responsible for visual target identification, view selection, action choice and recovery strategy. Deterministic code enforces reach, collisions, pickup/drop mechanics, state transitions, limits and final outcome. Do not add a second vision classifier or object metadata service to the agent path.

Use one model request per decision. Send the latest image only, with compact text summaries of prior actions. Store the model name, request ID, token usage, latency, tool call and frame path for audit. Validate numeric arguments against safe movement/turn ranges before execution; reject unknown tools or multiple calls. During tests, capture the actual outbound request payload and verify that no object IDs, colors, shapes, positions, counts or drop-zone coordinates appear outside the image bytes.

## 8. Complete product scope

### Product surfaces

| Surface | User action | Output |
| --- | --- | --- |
| Operator console | Enter a command, choose a scene seed, start a run | Live robot POV, state, elapsed time, step count, retry count, cost estimate |
| Run detail | Open a completed run | Outcome, timeline of camera frames and tool calls, errors, downloadable JSONL |
| Rehearsal view | Compare attempts for the same command | Success, steps, travel distance, collisions, cost and clip duration |
| Clip review | Preview and approve a generated MP4 | Format/duration/overlay checks and upload-ready artifact |
| Submission view | See the three hidden runs | Links to logs/clips, upload status, one-paragraph image-reasoning explanation |

The browser is an operator aid. Once **Start run** is pressed, the entire fetch must proceed without human control. The dashboard must not provide move/turn/pickup controls during a judged run. It may expose Stop only for emergency failure handling; a stopped run is a failure, not a success.

### Full-stack ownership

| Layer | Responsibility | Proposed technology |
| --- | --- | --- |
| Frontend | Command entry, live preview, status and artifact review | React + TypeScript + Vite |
| Product API | Run creation, status/event streaming, artifact listing, clip preparation, feedback upload | FastAPI + Pydantic |
| Agent worker | Claude tool loop, decision validation, state machine coordination, step and budget limits | Python + Anthropic SDK + `httpx` |
| Learning worker | Optional observation-only navigation/recovery policy, offline training and checkpoint evaluation | Python + PyTorch/Stable-Baselines3, isolated from engine evaluator data |
| Engine | Scene, render, robot movement, physics/collisions, private evaluator, required HTTP API | Unity 6 + C# |
| Database | Runs, steps index, artifact metadata, costs, upload status | SQLite, WAL mode |
| Artifact store | Camera PNGs, JSONL, raw POV frames, MP4s | Local `runs/<run_id>/` directories |
| Media | Assemble POV MP4 and command overlay | FFmpeg, invoked by backend |
| External APIs | Claude vision/tool use; organizer feedback API after sign-off | HTTPS adapters with keys in environment variables |

Single-machine deployment is intentional: all services and the game window run on the demo laptop. No cloud database, authentication provider or container orchestration is needed for the 24-hour event. Bind product/engine APIs to loopback; expose only the frontend locally. Never commit API keys, hidden commands or raw feedback credentials.

### User journeys

1. **Setup:** operator launches engine, backend and frontend. A health panel confirms camera, engine endpoints, Claude credentials, free disk space and FFmpeg. Reset creates a known scene seed.
2. **Practice run:** operator enters a command and presses Start. Backend creates a run record and invokes one agent worker. UI streams events and POV frames. Agent ends in a terminal outcome; backend finalizes logs and stats.
3. **Review:** operator sees frame-by-frame tool choices and failures, downloads logs, and decides whether to rerun with a new scene seed or adjust code. Review controls cannot alter an active run.
4. **Mentor demo:** operator starts a clean command, touches no controls, shows completion and evidence. Mentor sign-off is recorded manually in the console before Phase 2 controls unlock.
5. **Hidden command:** operator enters one published command exactly once, starts and records the run, then reviews the resulting clip without rerunning it.
6. **Submission:** operator validates and uploads each of three MP4s when the organizer API is available, confirms receipt, exports logs and copies the public repository link and explanation.

### Product API (proposed, separate from required engine API)

| Endpoint | Purpose |
| --- | --- |
| `GET /api/health` | Engine, camera, model credential and FFmpeg readiness |
| `POST /api/runs` | Start one command with optional scene seed and `practice`/`hidden` mode; returns run ID |
| `GET /api/runs` | List runs and high-level outcomes |
| `GET /api/runs/{id}` | Run detail and summary |
| `GET /api/runs/{id}/events` | Server-sent events for state, step, frame and terminal outcome |
| `GET /api/runs/{id}/frames/{step}` | Camera PNG for UI review; never used as object metadata |
| `GET /api/runs/{id}/log` | Download the run JSONL |
| `POST /api/runs/{id}/clip` | Generate and validate POV MP4 |
| `GET /api/runs/{id}/clip` | Preview/download MP4 |
| `POST /api/runs/{id}/submit` | Upload to organizer API after sign-off |

Only one run may control the engine at once; concurrent start returns a conflict. API errors use stable codes (`engine_unavailable`, `model_error`, `step_limit`, `clip_invalid`, `upload_failed`). A run ID is an opaque UUID, and backend paths are constructed from it rather than arbitrary client filenames.

### Data model and artifact layout

`runs`: id, command, mode, scene_seed, started_at, ended_at, status, outcome, steps, retries, collisions, distance_m, model_input_tokens, model_output_tokens, estimated_cost_usd, clip_status, upload_receipt. `steps`: run_id, step_number, timestamp, tool, args_json, result_json, frame_path, state_before, state_after, latency_ms. Store complete append-only step records in `runs/<id>/steps.jsonl`; SQLite indexes them for the UI. Store `frames/0001.png`, `frames/0002.png`, `pov.mp4`, and a `manifest.json` with file hashes and clip checks. Hidden commands and clips stay local until the authorized upload.

### State, consistency and failure handling

- Backend is the sole run coordinator; one worker owns the engine lease. A run moves through `queued → running → completed/failed/stopped`; terminal records are immutable except clip/upload fields.
- Engine controls robot state and physics. Backend stores a mirrored event timeline, not its own alternate physics state. If an action times out, query `/state` before retrying to avoid duplicate movement.
- Claude tool calls are schema-validated and restricted to permitted actions; no arbitrary code or filesystem tools. Reject multiple tool calls in a single decision and retry within a small bound.
- If the engine, Claude call or logger fails, end the run with an explicit reason and preserve partial logs. Flush JSONL after each step. Cap total run steps at 60 and collisions/recovery at the specified three retries.
- API secrets remain server-side. Frontend receives only run data and preview frames. Privileged engine evaluator fields never enter Claude requests, agent tool results or recent history.

### Reinforcement learning requirement

**Purpose:** improve low-level navigation and collision recovery while Claude remains the visual decision maker and issues exactly one tool call per step. This is an optional policy layer behind the baseline, not a replacement for the Claude loop. First ship the deterministic/Claude baseline and freeze it as a control. Then train and compare the learned policy on identical scene seeds and commands.

**Observation and action boundary:** the learning policy may use only the same public observation stream available to the agent: robot camera pixels, permitted robot pose/heading/holding/retry state, optional single-ray depth, and collision feedback. It must not consume object IDs, labels, colors, shapes, positions, counts, named-target distances, drop-zone coordinates, segmentation masks or engine-private evaluator values. It can propose a bounded `move` distance or `turn` angle, especially in `Blocked` or search states. Its proposal is advisory context to Claude; Claude's validated tool call remains the sole action sent to the engine. This preserves the required one-Claude-call-per-step and image-driven evidence.

**Training:** use a separate local simulation/training mode with randomized object placement, colors and scene seeds. Keep the action space small (for example, turn left/right 15–30°, move forward 0.2–0.5 m, reverse 0.1 m). Use sparse public rewards: successful drop `+1`, collision/retry penalty, step/time penalty, and terminal failure penalty. Reward computation may rely on the terminal pass/fail signal, but cannot expose the cause or object identity as an observation. Do not train on the three hidden commands or their post-event feedback before their one allowed run. Evaluate on held-out seeds and reasoning commands, measuring pass rate, steps, collisions and travel distance against the frozen baseline.

**Practical 24-hour plan:** begin with a small contextual bandit for recovery action selection or a compact PPO policy over the permitted observation space. A full visual end-to-end RL robot policy is a research project and should not block Phase 1. If training does not outperform baseline on held-out cases, disable it for judging. Log whether an RL suggestion was active and whether Claude followed it. If pulse results arrive in time, use them to adjust a documented policy setting and produce before/after evidence for the feedback-improvement stretch goal; otherwise choose another listed stretch goal. RL by itself is not an officially listed stretch goal.

### Video and feedback pipeline

Capture the robot POV while state is active, using the engine's rendered camera frames. Keep source timestamps and action sequence. FFmpeg encodes H.264 MP4 at 1280×720 or higher, with the command overlay for exactly the first two seconds and no team branding or code. Validation checks width, height, duration 10–20 seconds, playable stream and expected overlay. The backend uploads only after a local preview is approved. Implement the upload adapter once the organizer supplies its API guide and key; persist response ID/status and support safe retry if the server reports a transient failure. Do not assume an undocumented endpoint or payload.

### Quality and acceptance tests

| Test | Pass condition |
| --- | --- |
| Engine API contract | Correct units and fields; camera is 640×480 JPEG; no object data in public JSON |
| Boundary audit | Capture actual Claude requests for several runs; no object fields, object-encoded filenames or extra images |
| Autonomous run | Three different commands pass without input; include color/shape ambiguity |
| Collision recovery | Deliberate obstruction triggers Blocked and bounded backoff/turn/retry |
| Logging | Every decision has one JSONL line and matching PNG; terminal summary and cost available |
| Run safety | 60-step limit, one active run, API timeout behavior, malformed tool call handling |
| Product UI | Start, live status, frame/tool timeline, download, clip preview all work on demo laptop |
| Clip compliance | Three 720p+ MP4s, 10–20 seconds, command overlay first two seconds, no prohibited branding/code |
| Upload | Organizer API receipt stored; failures retry without duplicate submission where API permits |

### Repository and local deployment

Suggested layout: `engine/` (Unity project), `backend/` (FastAPI, agent, persistence, feedback adapter), `frontend/` (React app), `scripts/` (launch and clip validation), `tests/`, `runs/` (gitignored), `docs/` (API contract and judging explanation). Provide `.env.example`, dependency lockfiles and a one-command local launcher. README should cover prerequisites, startup order, test command, demo procedure, output paths and how to reset the scene. CI should run backend contract/unit tests and frontend build; engine scene smoke testing can run locally in Unity batch mode if available.

### Performance and operating targets

- Engine renders at least 30 FPS, while Claude decisions may take 2–5 seconds each. UI must remain responsive and show decision wait state.
- On the 6 GB system-RAM laptop, run the Unity standalone player for the live demo instead of the Editor. Keep only the player, Python backend and one browser tab open; if memory pressure persists, use the CLI command entry and omit the browser during judged runs. This is an operating fallback, not a change to the camera or API contract.
- Each action has a bounded timeout and visible failure reason. A full run stops at 60 decisions; track average steps and collisions to improve directness.
- Track per-run model tokens and estimated spend; warn at the $30 mentor threshold and reserve budget near 120 development runs.
- Allocate sufficient local disk space for frames/clips; prune practice artifacts only after review and never prune hidden-run evidence.

### Open product decisions

1. Confirm team familiarity with Unity and Unreal. Default to Unity unless the team can implement faster in Unreal.
2. Ask organizers whether a clip may be encoded from simulation-time frames to satisfy the 20-second limit when Claude wall-clock delays are long; preserve genuine action order.
3. Confirm exact Claude model ID and tool-use format from the supplied key.
4. Obtain the feedback API guide after mentor sign-off and finalize upload contract.
5. Confirm whether the live judge expects the browser console or accepts a CLI command entry; both must drive the same backend run path.

## 9. Delivery plan

1. **Scene and robot:** fixed scene seed, visible objects/zone, camera image, collision-aware movement.
2. **Engine API:** endpoint contract, state machine and private evaluator; validate that public responses carry no object data.
3. **Agent plumbing:** dummy tool policy through complete runs, JSONL/PNG logging, frame sequencing and 60-step stop.
4. **Claude loop:** image content blocks, one-tool response validation, command reasoning, recovery and cost tracking.
5. **Phase 1 rehearsal:** at least three varied commands, no manual intervention, review frame/log evidence, mentor sign-off by hour 14.
6. **Learning experiment:** freeze baseline, train the observation-only recovery/navigation policy, compare on held-out seeds, and enable only if it measurably improves success or directness without losing reliability.
7. **Phase 2:** receive API guide, implement upload, capture compliant clips, execute hidden commands once at hour 18, upload by hour 20.
8. **Submission:** publish engine and agent source, learning experiment results if used, hidden-run logs, one-paragraph image explanation and demo booking.

## 10. Risks and decisions

- **Object leakage:** audit every agent-facing field and prompt before Claude calls. Private debug views must not be reused in responses.
- **Camera ambiguity:** duplicate colors/shapes require wide enough views and deliberate turn/search behavior; test reasoning commands early.
- **Slow calls:** 2–5 seconds per image decision can make clips exceed 20 seconds. Record simulation-time POV at 30 FPS, then encode a 10–20 second clip while preserving the actual action sequence and organizer rules; confirm acceptability with mentors before relying on time compression.
- **HTTP/render thread:** queue API actions onto the engine's scene thread so captures and physics state remain consistent.
- **Budget:** target $40, alert at $30; stop development runs around 120 to reserve tests and rehearsal.
- **Unknown feedback API:** keep upload implementation behind a small adapter until the guide is issued.

## 11. Explicit non-goals for baseline

No grasping physics, furniture, private sensors, object-detection metadata, map coordinates given to Claude, cloud hosting, multi-user collaboration or public accounts. These can be considered only if required for a selected stretch goal and allowed by mentors. The operator console is in scope, but manual robot driving during a judged run is not.

## 12. References

- Hackathon source: `membrane_only_track_brief.pdf` (local supplied brief).
- Godot: [ViewportTexture](https://docs.godotengine.org/en/stable/classes/class_viewporttexture.html), [Image JPEG encoding](https://docs.godotengine.org/en/stable/classes/class_image.html).
- Unity: [Camera target texture](https://docs.unity3d.com/6000.0/Documentation/ScriptReference/Camera-targetTexture.html), [JPEG encoding](https://docs.unity3d.com/6000.0/Documentation/ScriptReference/ImageConversion.EncodeToJPG.html).
- Panda3D: [render to texture](https://docs.panda3d.org/1.10/python/programming/render-to-texture/low-level-render-to-texture).

- Unreal: [Scene Capture 2D](https://dev.epicgames.com/documentation/en-us/unreal-engine/BlueprintAPI/Rendering/SceneCapture/CaptureScene).


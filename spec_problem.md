# Membrane Robot Hackathon — Problem Statement Spec

Source: `Membrane_Robot_Hackathon_Problem_Statement.docx`, dated **Nov 2, 2026**. 24-hour event.
This file restates every detail of that document. Section 14 maps each requirement to this project's implementation.

---

## 1. The challenge

**Build a robot that fetches what you ask for, using nothing but its own eyes.**

Your team builds a simulated robot in any game engine, and a Claude agent that drives it.

- A user types a command, for example *"Bring the red cube to the green square."*
- Claude looks through the robot's camera and works out which shape is the red cube.
- Claude steers the robot to fetch it and drop it in the zone.

There are no coordinates, no object list and no shortcuts. Claude gets just pixels, a depth reading and a collision flag.

- **Phase 1** is the robot and agent, and it is most of your score.
- **Phase 2:** you record what the robot saw and send the clip through a human feedback API. Real users watch it and tell you whether the robot did the job.

## 2. Why this problem

Real robots fail in ways their own logs never show.

- The log says "object delivered".
- The video shows the robot scraped a wall, shoved another object aside, and dropped the cube half outside the zone.

Robotics teams pay human raters to watch exactly this footage, because the only honest judge of how a robot behaves is a person who watches it.

The hackathon is a small version of that loop:

- **Phase 1 is the robotics problem:** see, decide, move, with no cheats.
- **Phase 2 is the feedback problem:** a human feedback API puts the robot's footage in front of real users within minutes and returns their verdict.

You close a loop that a real robotics lab needs weeks to set up.

---

## 3. Phase 1: Build a robot Claude can drive

**Goal:** a typed command goes in, and a completed Fetch comes out, with no human help in between.

**Target:** working by **hour 14**. Nothing in Phase 2 can start without it.

Phase 1 is image-driven.

- The only thing Claude knows about the world is what the robot's camera shows.
- It may also use two optional sensors: a **depth reading** and a **collision flag**.
- **No object data of any kind enters the system from the engine.**

### 3.1 Architecture (diagram caption: "Phase 1 architecture · 3 processes, 1 cloud API")

Each layer knows only what the layer before it hands over:

- The **engine** knows everything about the scene.
- The **agent** knows only what the engine returns over HTTP.
- **Claude** knows only what the agent sends it.

Object data stops at the engine boundary.

| Process | Role |
| --- | --- |
| **Engine process** | Runs the scene at **30 fps or more**, renders the robot camera to a texture, and hosts a small HTTP server on **`localhost:8080`**. It owns physics, collisions and the state machine. |
| **Agent process** | A **Python script**. It holds the command, runs the Claude tool-use loop, calls the engine endpoints and writes the log. Nothing else. |
| **Claude API** | Receives the command, the latest frame, the depth value and the recent history. Returns **exactly one tool call per step**. |

### 3.2 The control loop

- [ ] One Claude call per step.
- [ ] Expect **15 to 40 steps per run**. Each Claude call takes **2 to 5 seconds** with an image.
- [ ] **Freeze the engine clock while Claude thinks.** The scene advances only during `/move` and `/turn`. Real robot motion then totals **10 to 20 seconds per run**, which is your clip length.
- [ ] Send the `/look` image to Claude as an **image content block inside the tool result**. Do not describe the image in text.

### 3.3 Data contract

| Item | Specification |
| --- | --- |
| Units | Metres for distance, degrees for angles. Heading 0 = +x axis, positive = clockwise from above. |
| `/look` image | JPEG, **640 x 480**, **90°** horizontal field of view. Camera at the robot's front, **0.5 m** above the floor, tilted **10° down**. Fixed for the whole hackathon. |
| `/depth` value | Single ray from the camera centre. Float metres, **max 10**. `null` if nothing is within range. |
| `/state` body | `{"name": "Carry", "x": 2.1, "y": 0.4, "heading": 90, "holding": true, "retries": 0}` |
| `/move` | Body `{"distance": 1.0}`. Negative = reverse. Speed **1 m/s**. Returns `{"moved": 0.6, "collided": true}`. |
| `/turn` | Body `{"degrees": 30}`. Positive = right. Speed **90°/s**. Returns `{"heading": 120}`. |
| `/pickup` | Reach **0.5 m**, within **±30°** of heading. Returns `{"ok": true}`. **Never says which object.** |
| `/drop` | Returns `{"ok": true, "in_zone": false}`. |
| Collision | The robot has a physics collider. On contact the move stops, the robot **backs off 0.1 m**, `collided` is true, and the state machine enters **Blocked**. |
| Log | **One JSONL line per step:** `step, timestamp, tool, args, result, frame_path`. **Frames saved as PNG next to the log.** |

You may tune the numbers above for your engine. **Write your values in your README.** Judges will check that the camera, not the engine, is the only source of object information.

---

## 4. The task: Fetch

The robot must bring **one named object** to **one marked drop zone** on a single plain-language command.

### 4.1 Scene

- **One flat room with four walls. No furniture.**
- **4 to 6 primitive shapes** on the floor: cubes, spheres, cylinders and cones, in different colours. **At least two share a colour and at least two share a shape**, for example a red cube, a red sphere and a blue cube.
- **One marked drop zone:** a coloured square painted on the floor, or a flat platform.
- **One wheeled robot with a forward-facing camera.** A capsule with a camera is enough.

### 4.2 Command

A person types one sentence to Claude, for example:

- "Bring the red cube to the green square."
- "Fetch the blue sphere."

Judges will also give commands that need some reasoning, such as:

- "Bring the round red thing."
- "Bring the tallest object."

The hidden-command example is "Bring me something to drink."

### 4.3 Robot behaviour

- The robot finds the object **with its camera only**. It does not get object positions, names, colours or shapes from the engine.
- The robot drives to the object, picks it up, drives to the drop zone and releases it.
- **Pick-up is a snap.** When the robot touches the object, the object attaches to it. No grasping physics is needed.
- **The engine must stop the robot when it hits a wall or another object, and tell it that it hit something. Collisions must be visible in the clip.**
- Optional sensors are a depth reading (distance to the nearest thing straight ahead, in metres) and a collision flag (true or false). **Nothing else.**

### 4.4 Rules

- Any game engine is allowed: Unity, Godot, Unreal, Three.js, PyBullet, or your own.
- Primitive shapes and free assets are allowed. You do not need a 3D artist.
- **No object data may enter Claude's context from the engine.**
  - This covers names, ids, colours, shapes, positions, distances to objects and object counts.
  - It applies everywhere: `/state`, the system prompt, tool descriptions and file names.
  - Everything Claude knows about the objects comes from the pixels in `/look`, plus the optional depth reading and collision flag.
- The depth reading tells the robot how far the nearest thing ahead is. **It does not say what the thing is.**
- **One command per run. One run per clip.**
- A run ends when the robot reaches the drop zone with an object, or when it gives up after **3 retries**.

---

## 5. Robot states (diagram caption: "robot states · 7 states, 1 retry branch")

The robot is in exactly one state at a time. **Your clip starts when it leaves Idle and stops when it reaches Done.**

**Blocked** fires when the robot has not moved for **2 seconds**, or has hit something. After **3 retries** the run ends in Done with a fail result.

| State | Enters when | Leaves when | Who decides |
| --- | --- | --- | --- |
| Idle | Scene is loaded | Claude sends a command | Claude |
| Locate | Command is parsed | Target object is identified in the camera view | Claude |
| Go to object | Target is known | Robot is within reach | Claude or robot code |
| Pick up | Within reach | Object is attached | Robot code |
| Carry | Object is attached | Robot is inside the drop zone | Claude or robot code |
| Drop | Inside the drop zone | Object is released | Robot code |
| Done | Drop finished, or retries used up | Result is reported | Robot code |
| Blocked | No movement for 2 s, or collision | Path is clear, or 3 retries used | Robot code |

Done always writes exactly one result: **`pass`, `fail_wrong_object`, `fail_not_reached` or `fail_collision`**. These match the pulse labels, so you can compare your own log with the crowd's verdict.

---

## 6. Robot API and Claude agent

There are two parts: the robot in the engine, and the Claude agent that drives it. You build both.

### 6.1 Robot API (engine side)

The engine must expose a small local HTTP server. Claude talks to the robot only through it.

| Endpoint | Returns or does |
| --- | --- |
| `GET /state` | Current state name, robot x, y, heading, whether it holds something, and the retry count. **Nothing about the objects.** |
| `GET /look` | One JPEG frame from the robot's camera, as the robot sees it now. **This is Claude's only view of the objects.** |
| `GET /depth` | Optional. Distance in metres to the nearest thing straight ahead. Does not say what it is. |
| `POST /move {distance}` | Drive forward or back by a distance in metres. Stops early on collision and returns `collided: true`. |
| `POST /turn {degrees}` | Turn left or right in place. |
| `POST /pickup` | Attach the nearest object if it is within reach. Returns success or failure only, not what was picked up. |
| `POST /drop` | Release the held object. Returns whether it landed in the drop zone. |

You may add `POST /go_to {x, y}` so the engine handles path-finding. Claude still has to find the object first with `/look`.

### 6.2 Requesting extra sensors or endpoints

- You may ask for more. **A mentor approves or rejects within 15 minutes.**
- The test is one question: *would a real robot have this sensor, and does it stay silent about what an object is?*
- **Likely approved:**
  - a multi-ray 360° depth scan (lidar-style)
  - bump sensors per side
  - a second or wider camera
  - a downward camera for the drop zone
  - wheel odometry
  - an IMU
- **Always rejected:** anything that returns object ids, names, colours, shapes, positions, counts, distance to a named target, segmentation masks, or drop-zone coordinates.
- **Every approved sensor is published to all teams within the hour. No private sensors.**

### 6.3 Claude agent

- Use the Claude API with tool use. Each tool calls one endpoint above.
- Claude receives the command, calls `/look`, reads the image, decides what to do, and loops until Done.
- Claude must identify the object and the drop zone **from the image**. It must work out where they are by looking, turning and looking again.
- **The system prompt may describe the robot and its tools. It may not describe the room or the objects in it.**
- **Keep a log of every tool call and every image Claude saw. Judges will read it.**

---

## 7. Your Claude budget (not a strict budget)

Each team gets **one API key with a $40 target**.

- There is no hard block.
- **Mentors get an alert at $30** and will talk to you.
- The limits below are how you stay inside $40 without thinking about money.

| Limit | Number | Why |
| --- | --- | --- |
| Images per step | **1** | One `/look` before each decision. Never two in a row without a move or turn between them. |
| Claude calls per step | **1** | One call in, one tool call out. |
| Steps per run, soft target | **25** | A clean run: 4 to 6 looks to find the object, 6 to 10 to reach it, 6 to 10 to carry it, and 2 for pickup and drop. |
| Steps per run, hard stop | **60** | The agent reports fail at step 60. No exceptions. |
| Cost per step | **~$0.009** | One 640 x 480 image plus prompt and recent history on Sonnet 5.5. |
| Cost per run | **~$0.25 at 25 steps, ~$0.55 at 60** | Long runs cost double. Fix the agent; do not let it wander. |
| Runs in $40 | **~150** | 100 development, 40 rehearsal, 3 hidden commands, and the rest is buffer. |

### Rules that keep you inside the budget

- **When to look:** after every `/move` or `/turn`, once. Not after `/pickup` or `/drop`, because their result already tells you what happened.
- **How to find the object:** turn 45°, look, and repeat. Eight looks cover the room. If the object is not in any of them, drive 2 m toward open space and sweep again.
- **Send only the latest frame to Claude.** Earlier steps go in as one line of text each. Five images in history makes every call five times more expensive and no smarter.
- **Put the system prompt and tool definitions first and enable prompt caching.** Cached input costs 10% of normal.
- **Test your HTTP plumbing with Claude Haiku 5.5** or with a dummy agent that returns fixed moves. Do not spend Sonnet calls finding out whether your server starts.
- **Count your runs.** At 120 runs, stop developing and keep the rest for rehearsal and the hidden commands.

---

## 8. Phase 1 is complete when

- [ ] You type a command and the robot ends in Done with `pass` on **at least 3 different commands**.
- [ ] Claude found each object from `/look` images, and your tool-call log shows it.
- [ ] **Nobody touched the keyboard or mouse during the run.**
- [ ] You demo this live to a mentor. When the mentor signs off, you receive your **human feedback API key and the API guide**. Phase 2 starts there.

---

## 9. Phase 2: Connect the robot to Membrane

**Goal:** real users watch your robot and tell you how it did.

- You record the camera view and send the clip through the human feedback API.
- You get the API key only after a mentor has signed off your Phase 1 demo.

### 9.1 Record the POV clip

- Record the **robot camera view**. Screen capture of the camera window is fine, and so is OBS.
- Format: **mp4, 720p or higher, 10 to 20 seconds. No audio needed.**
- **Start recording when the state leaves Idle. Stop when it reaches Done.**
- **Show the typed command as text on screen for the first 2 seconds**, so the viewer knows what the robot was asked to do.
- **No team names, logos or code visible in the clip.**

### 9.2 How the crowd judges you

- Users watch your clip and answer **two short questions**. Each question is a "pulse".
- They see only the video. They know nothing about your team or your code.

| Pulse | Type | Question / options |
| --- | --- | --- |
| Pulse 1 | Binary | "Did the robot bring the correct object to the drop zone?" Yes or no. |
| Pulse 2 | Labels | "What went wrong?" Pick one: **hit something, wrong object, did not reach the drop zone, too slow, nothing wrong**. |

### 9.3 Hidden test commands

- You develop with your own commands for the **first 20 hours**.
- **At hour 18**, judges publish three commands nobody has seen, for example "Bring me something to drink."
- You run each command **once**, record it, and **upload the three clips by hour 20**.
- Only these three clips go to the pulses. Hard-coding a single path will not help you.

### 9.4 Results

- Each clip gets **at least 30 user responses**.
- You receive a result sheet per clip, with the **yes-rate for pulse 1** and the **label split for pulse 2**.
- Results come back **within 45 minutes of upload**. Use them for the stretch goal or for your demo.

---

## 10. Milestones over 24 hours (diagram caption: "24-hour plan · 6 milestones, 2 gates")

- Phase 1 fills the **first 14 hours**.
- Phase 2 has **two hard gates**:
  - **hidden commands are published at hour 18**
  - **clip upload closes at hour 20**
- Everything else is a suggested order.
- Teams that reach hour 14 with Claude driving the robot have time for the stretch goal.
- **Teams that spend more than 8 hours on the engine should simplify the scene, not the agent.**

## 11. Stretch goals (any one counts)

- Read the pulse result sheet and change the robot's behaviour before the demo. Show the before and after clips.
- Add furniture or walls inside the room that block the straight path to the object.
- Claude plans the full path itself with move and turn only, with no `go_to`.
- Commands that need reasoning across several views, for example "Bring the cube that is farthest from the green square."

## 12. Scoring

Phase 1 decides **65%** of your score, Phase 2 adds **20%**, and stretch goals add **15%**.

| Part | Weight | How it is measured |
| --- | --- | --- |
| Phase 1: Fetch works | **40%** | Live demo on **two judge commands**. Pass or fail per command, plus how directly the robot went. |
| Phase 1: image-driven agent | **25%** | Judges read your tool-call log and image log. Did Claude find the object from pixels, recover from a collision, and reason about the command? |
| Phase 2: user verdict | **20%** | Yes-rate on pulse 1 and "nothing wrong" share on pulse 2, averaged across your 3 clips. |
| Stretch goal | **15%** | Any one stretch goal shown working in the demo. |

**Penalties:**

- A team with **no clips uploaded by hour 20 scores 0 on Phase 2**.
- **Any object data found leaking into Claude's context scores 0 on both Phase 1 parts.**
- Clips that break the rules (team name on screen, over 20 seconds) are **not sent out and score 0 on Phase 2**.
- **Ties are broken by the Phase 1 demo score.**

---

## 13. Values we tuned (the brief asks for these in the README)

| Item | Brief | This engine |
| --- | --- | --- |
| Pickup reach | 0.5 m, ±30° | Object centre within **0.86 m of the robot centre** (about 0.46 m beyond the 0.4 m body) and within ±30°. It snaps between the gripper fingers. |
| Collision back-off | 0.1 m | 0.1 m, animated over a few frames so it shows in the clip. Moves are checked for both the body and the gripper. Turns are also checked, so the gripper or a held object cannot swing through things. |
| Depth | Single ray from the camera centre | A level sphere-cast (radius 0.12 m) from the camera, so low objects register and open floor reads `null`. |
| `/move` limit | — | ±3 m per call in the engine; the agent tool allows ±1 m. |
| Drop zone | Square or platform | Green 3.6 × 1.5 m. |
| Speed | 1 m/s, 90°/s | Same at 1x. The 2x and 4x buttons are for demos only; record judged clips at 1x. |
| Scene | Plain room, no furniture | Warehouse room with the play area cleared. Six primitives: red sphere, red cube, blue sphere, blue cube, yellow cylinder (the tallest) and yellow cone. Two of them sit far from the zone. |

## 14. Where each requirement lives in this project

| Requirement | Implementation |
| --- | --- |
| Engine, HTTP :8080, state machine, collisions | `E:\turtlebot3-unity-rossharp-main\...\Assets\Scripts\RobotEngine.cs` (scene `Tbot3SampleEnv`) |
| Agent loop, one image and one tool per step, 60-step stop, JSONL and PNG log | `agent/run_agent.py` |
| Budget rules: caching, latest frame only, no look after pickup or drop, run counter, cost per step | `agent/run_agent.py` |
| Phase 2 POV clip (1280×720 mp4, 10–20 s, 2 s command overlay) | Engine recorder (`record_dir` on `/start`) and `agent/clip.py` |
| Built-in non-Claude runner for demos and tests | `agent/builtin.py`, engine `/run_builtin` |
| Live acceptance tests | `tests/test_phase1_live.py` |
| Offline tests | `tests/test_agent.py`, `tests/test_clip.py` |
| Upload to the Membrane API | **Not built.** The API key and guide come after mentor sign-off. |

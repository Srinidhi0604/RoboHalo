# RoboHalo: 30 technical judge questions

Focused questions and descriptive answers for the Phase 1 robot task and the separate warehouse product demo.

## Architecture and scenes

### 1. What are the three processes?

**Unity** renders and simulates the room, robot, camera, physics, collisions, and state machine. **Python** holds the command, calls Unity's local HTTP API, sends permitted observations to the model, validates tool decisions, and writes evidence. **Claude API** examines the latest observation and returns one action. The model is a decision-maker, not the physics engine or the source of ground-truth object data.

### 2. What is the difference between your scenes?

The **RoboHalo baseline** in `engine/Assets/Scenes/RoboHalo.unity` is the simple fetch task with repeated colors and shapes. The **Warehouse** scene in `engine/Assets/Scenes/Warehouse.unity` is a richer, procedural operations demonstration with pallets, conveyors, and four robots. A **separate local TurtleBot3 Unity/ros-sharp project** is another integration; it is not automatically the same as the version-controlled `engine/` project. Name the scene you actually show before discussing its behavior.

### 3. Why is the warehouse scene not your default judged scene?

The supplied Phase 1 brief calls for one flat room, four walls, no furniture, four to six primitive objects on the floor, and one wheeled robot. The warehouse has pallets, conveyors, many boxes, and multiple robots; its lift pickup range also differs from the nominal snap-on-contact behavior. It is useful for a product pitch, but those differences must be disclosed or approved by a mentor before treating it as the judged configuration.

## Perception and data boundary

### 4. How does Claude know which object to fetch?

In the judged path, the agent sends Claude the user's command and the latest JPEG from the robot's forward camera. Claude must infer color, shape, relative direction, and later the drop zone from pixels, using turns and fresh looks when needed. The engine may privately resolve the target to score the final drop, but it must not tell the model the target's identity or location.

### 5. What can Claude receive from Unity?

The permitted channels are the robot-camera image, an optional **single-ray** forward distance, a collision flag/action result, and robot state such as heading, position, holding status, retries, and terminal outcome. The state is about the robot, not a catalog of objects. Earlier actions can be summarized in short text history.

### 6. What information is prohibited?

Do not pass object names, IDs, colors, shapes, coordinates, counts, named-target distances, segmentation masks, or drop-zone coordinates through `/state`, prompts, tools, error strings, filenames, or hidden metadata. The camera pixels are the source of object information. Even a helpful-sounding tool description such as “move toward the red cube at x=2” would violate the rule.

### 7. How do you prove no object data leaks into the model context?

Inspect the constructed model request: system prompt, tool definitions, user message, image block, action history, and tool results. The repo includes tests that scan for scene words in this payload, and the engine reply types contain robot data rather than object fields. For a serious audit, retain the exact request/response trace for a live run and inspect it manually as well; static tests alone cannot prove every runtime path.

### 8. What is the required camera contract?

The brief specifies a 640×480 JPEG, 90° horizontal field of view, mounted at the robot's front 0.5 m above the floor and tilted 10° down. A 4:3 camera needs roughly 73.74° vertical field of view to obtain 90° horizontally. The fixed camera contract should be checked from a live `/look` frame and scene settings, not inferred from a screenshot of the Unity overview.

### 9. Does `/depth` identify an object?

No. It returns only the distance, in metres, to whatever a single ray straight ahead hits, up to 10 m, or `null` when nothing is in range. It cannot say whether the hit is a wall, crate, target, or another robot. The camera remains responsible for semantic identification.

### 10. How often does the agent look?

It gets an initial frame and then refreshes the view after a move or turn. It does not request a redundant new frame immediately after pickup or drop, because those tools already return the outcome needed for the next decision. It sends only the latest frame to the model rather than accumulating a stack of old images.

## Robot and engine

### 11. What endpoints does the engine expose?

The core local API is `GET /state`, `/look`, `/depth` and `POST /move`, `/turn`, `/pickup`, `/drop`, with `/start` to begin a command and `/reset` for a fresh run. The engine listens on `127.0.0.1:8080` in the baseline. The optional `/go_to` is a navigation convenience in one engine; it must not reveal the target or remove the need to find it from the camera.

### 12. What is the state machine?

The intended sequence is **Idle → Locate → Go to object → Pick up → Carry → Drop → Done**. **Blocked** is the retry branch for a collision or lack of progress. The engine owns these transitions and the final outcome; Claude chooses one next tool at each decision point rather than setting an arbitrary state string.

### 13. What happens when the robot collides?

For a blocked move, the engine stops motion early, reports `collided: true`, backs off about 0.1 m, and increments the retry count. The model can then turn or reverse and try another route. After three collision retries, the run should finish with `fail_collision`. The visible clip should show the stop rather than an object passing through another object.

### 14. How does pickup work?

The baseline uses a simplified snap pickup: an eligible object near the robot and within a forward angle is attached to a hold point. It does not simulate a physically realistic gripper. The reference brief uses approximately 0.5 m reach and ±30°; the warehouse lift scene uses a larger 1.6 m reach to access pallet stacks, which is a disclosed deviation.

### 15. How is the drop scored?

The engine checks whether the held object was released in the marked zone and whether it was the requested object. A correct item in the zone is `pass`; a wrong item is `fail_wrong_object`; dropping outside or giving up is `fail_not_reached`; three collision retries yield `fail_collision`. The action reply may report `in_zone`, but it must not reveal object identity.

### 16. How do you freeze the simulation while Claude thinks?

The design pauses simulated time between actions, so a slow network/model response does not make the belt, objects, or robot drift. In the warehouse API mode, the code sets Unity time scale to zero while no API action is active, then advances during an action. This needs a live timing check because a source-level condition does not prove every coroutine behaves as intended.

## Agent and model control

### 17. What does the Python tool-use loop do on each step?

It asks Unity for permitted state and, when due, the latest camera frame and depth; sends one model request; accepts at most one valid tool; calls the matching engine endpoint; then records the result. It repeats until the engine reaches Done, the model chooses to fail, an API error ends the run, or the hard step limit is reached. This is the loop in `agent/run_agent.py`.

### 18. Why require exactly one tool call?

One action at a time keeps the run auditable and lets the next decision use the consequence of the previous action. Parallel actions could make a robot move and pick up based on a stale image. The current request disables parallel tool use, but the response parser takes the first tool call if a model still returns several. **Before claiming strict compliance, change that parser to reject any response with more than one tool call and add a test.**

### 19. What is the step budget?

The brief suggests about 25 steps for a clean run and a hard limit of 60. The Claude runner uses a 60-step ceiling. Free-provider test runs may use a shorter default, so announce the selected provider and limit when showing logs. The step cap controls cost and prevents endless wandering.

### 20. What is `/go_to`, and is it allowed?

The brief explicitly allows an optional `POST /go_to {x,y}` for engine pathfinding if the model first discovers the object using `/look`. It can simplify motion planning while preserving visual target identification. The plain move-and-turn route is stronger evidence for a path-planning stretch goal. The procedural warehouse API does not currently expose `/go_to`.

### 21. Why use Claude if Groq can be free?

The supplied challenge explicitly specifies **Claude API with tool use** for the judged agent and provides a team key. Groq is useful for development and for the separate Operations interpretation bridge, but a Groq-only clip is not a substitute for the required Claude run unless a mentor authorizes it. We can demonstrate shared engine plumbing with Groq while keeping the judged path on Claude.

### 22. How do you reduce model cost and latency?

The judged runner sends only one current image, compact action history, and a constrained tool set; it avoids unnecessary new looks after pickup/drop. The warehouse Operations bridge skips the model entirely for explicit orders. The brief also recommends prompt caching and testing HTTP with a fixed runner or a cheaper model before spending Claude calls; report measured cost and latency from actual logs rather than assuming the brief's estimate.

## Warehouse operations

### 23. What do Atlas, Bolt, Comet, and Dash do?

Atlas and Comet are the two **selector** robots that take requested cargo from pallet aisles to the moving belt. Bolt and Dash are **collector/sorter** robots that take cargo from the end belt to collection lanes. The procedural scene separates each selector's aisle region and gives the operator a camera/watch control.

### 24. Why did the robots previously start moving without a command?

The warehouse scene used to import a `demo.csv` priority list automatically and run selector loops that chose any top crate when no order matched. The current source removes automatic CSV import and makes selectors wait for a requested matching item. Importing the CSV is now an explicit operator action.

### 25. How does a typed warehouse request reach the robots?

The existing Unity Operations text field posts the sentence to the Python bridge at `127.0.0.1:8091/interpret`. The bridge returns a constrained intent, or Unity uses its local parser if the bridge is unreachable. Unity queues the parsed order; Atlas and Comet select only top-of-stack cargo matching the requested color and shape. Their movement and belt handoff remain in Unity.

### 26. How do you preserve request order with two selectors?

The warehouse code queues incoming requests. It tracks unclaimed units and units already being carried; the next request becomes active only after all units of the current request reach the belt. Both selectors may work on the same active request, but neither should claim a crate for a later request early. A live multi-order demonstration remains the final check for this behavior.

## Evidence and readiness

### 27. What does Phase 2 require?

After mentor sign-off, record one robot-POV MP4 per run, at least 720p and 10–20 seconds long. Begin when the state leaves Idle and stop at Done. Show the typed command for the first two seconds, and exclude team names, logos, code, and audio requirements. The clip tool can validate format and duration, but the exact judged clip still needs visual review.

### 28. Is the Membrane feedback API fully connected?

No live end-to-end upload is established in the current local code. There is upload/result scaffolding, clip validation, and a hidden-run runbook, but the actual transport needs the official API guide and the issued key after sign-off. Do not invent endpoint URLs or claim a crowd result without an upload receipt and returned sheet.

### 29. How do you handle API credentials safely?

Keep Claude, Groq, and eventual Membrane keys in environment variables or ignored local `.env` files; never put them in Unity assets, source, logs, screenshots, or Git. The screenshot shared in this chat appears to show a live Claude key posted in a group conversation. Treat that key as exposed: ask the key owner/organizer to revoke and replace it, then use the replacement privately. This document deliberately does not reproduce any key.

### 30. What tests have you run, and what remains unverified?

The repo includes offline tests for agent payloads/tool behavior, warehouse source rules, order parsing, clips, and secrets. The last warehouse change compiled with zero C# errors and its focused Python tests passed; one Groq interpretation was tested live through the local bridge. The Unity editor had not reloaded that change and its HTTP endpoint timed out, so a fresh Play-mode check of robot motion, request order, frame rate, and final outcomes is still required.

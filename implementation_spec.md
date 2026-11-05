# RoboHalo Remaining Implementation Specification

Status: implementation backlog based on the Membrane Robot Hackathon problem statement  
Source: Membrane_Robot_Hackathon_Problem_Statement.docx  
Related documents: spec.md, spec_problem.md, PRD.md

## 1. Purpose

This document defines the work still needed to satisfy the hackathon brief and prepare RoboHalo for a reproducible demo. It distinguishes code that exists from behavior that still needs implementation or live evidence. A script compiling or a test run succeeding does not satisfy a camera-driven acceptance gate.

## 2. Current implementation baseline

The repository contains a Unity 6000.6.3f1 baseline scene and local HTTP engine on port 8080; a TurtleBot3 visual, primitive fetch objects, drop zone, camera, movement and collision handling; a Claude tool loop with constrained actions and JSONL logs; test runners for movement tests; and POV clip assembly with offline tests.

The active warehouse project is separate from this repository and is not currently reproducible from a clean clone. Verify each implementation claim against the exact scene used for the live demo. Test runners are for plumbing checks and do not count as visual reasoning.

## 3. Workstream A: Phase 1 acceptance

### A1. Lock the judged scene and camera contract

Select one reproducible Unity scene as the judged baseline. Confirm robot camera JPEG output at 640×480, 90° horizontal field of view, 0.5 m height and 10° downward tilt. Confirm permitted task objects and drop zone. Keep object names, identities, positions, shape/color labels and counts out of agent-visible responses. Maintain at least 30 FPS during motion. Ensure the agent sees only robot POV frames.

Acceptance:
- Record Unity version, scene path and camera values in README.
- Capture a /look frame and verify resolution and view.
- Clone the repository into a clean directory and open the same scene without local-only files.

### A2. Freeze simulation during model deliberation

Pause simulated time while Python waits for Claude. Advance robot/world only during /move and /turn; camera and HTTP calls must remain responsive. Use a controlled simulation clock or explicit movement coroutines. Never block Unity’s main thread on a model response.

Acceptance:
- A multi-second Claude request causes no stationary-scene or physics movement.
- A /move of distance d takes approximately d seconds at 1 m/s; /turn of a degrees takes approximately |a|/90 seconds.
- /state, /look and /depth remain responsive.

### A3. Enforce the observation and action boundary

Send Claude only the original command, latest image, optional forward depth, permitted robot state, collision/action result and compact history. Include the image as an image content block, never a textual scene description. Make one Claude request and execute at most one tool call per decision. Do not send object data through state, errors, schemas, logs, filenames or history. Engine may use private target data for scoring.

Acceptance:
- Automated payload audit covers system prompt, tool definitions, message history and tool results.
- Run-log audit confirms every image sent to Claude has a saved image file and matching entry.
- A reviewer can trace decisions to frames without finding privileged scene metadata.

### A4. Complete camera-driven fetch validation

Complete at least three different natural-language tasks with Claude, without input after starting each run. Include reasoning commands such as “Bring the round red thing” and “Bring the tallest object.” Demonstrate a visible collision stop and recovery. Three failed retries must end with fail_collision. Verify pass, fail_wrong_object, fail_not_reached and fail_collision.

Acceptance:
- Preserve three successful run folders with command, JSONL trace, sent frames and final state.
- Preserve one collision recovery recording and its log.
- Conduct the live mentor demo and record sign-off before treating Phase 2 access as available.

## 4. Workstream B: Phase 2 evidence clips

### B1. Enforce recording boundaries and format

Start recording when state leaves Idle and stop at Done. Record robot POV at 1280×720 or higher, MP4, 10–20 seconds. Show the typed command for the first two seconds. No team names, logos or code in frame. Produce one clip per run.

Acceptance:
- Automatically inspect dimensions, duration and readable frame count.
- Reject invalid clips and state which criteria failed.
- Review overlay and video content, including visible collision events.

### B2. Implement Membrane upload and result retrieval

Prerequisite: receive the mentor-approved API key and official API guide. Do not guess endpoint paths, headers, schemas or pulse IDs.

Deliverables:
- Client uploads a validated clip and required metadata.
- Credentials load from environment variables or ignored local config; never log or commit keys.
- Capture submission ID/status and useful errors.
- Retrieve both pulse results, preserving raw data and summarizing yes-rate and failure-label distribution.
- Retry transient failures without duplicate submissions.

Acceptance:
- Upload a compliant sample using the official guide and confirm it appears in service.
- Retrieve its result sheet; verify counts and aggregation.
- Confirm the key is absent from repository history, logs and artifacts.

### B3. Hidden command submission runbook

At release, record three hidden commands verbatim. Run each exactly once in a fresh run directory with a separate clip. Upload all three before the cutoff. Keep logs and upload receipts.

Acceptance: checklist records command, single run, outcome, clip validation, upload ID and status for each.

## 5. Workstream C: stretch goal

Choose one goal: add an obstacle requiring a camera-driven detour using move and turn; support multi-view reasoning such as selecting the cube farthest from the zone; or use crowd feedback to change behavior and show before/after clips.

Acceptance:
- Works on a fresh run without exposing scene metadata to Claude.
- A demo clip, trace and short explanation demonstrate the behavior.

## 6. Workstream D: reproducible warehouse project and asset handling

If the warehouse is the final demo, bring its scene, TurtleBot3 assets, RoboHalo scripts and packages under this repository. Preserve Unity .meta files and GUIDs. Include a clean setup path and exact editor version. Preserve third-party licenses and attribution; confirm redistribution terms before committing the full asset pack. Keep generated caches, builds, videos and keys out of Git.

Acceptance:
- Clean clone opens warehouse scene and reproduces demo.
- One intended controller/camera path; no duplicate port 8080 server.
- Objects, zone, robot, routes and reset work without obstructing props.
- Asset credits and license notices are present.

## 7. Verification checklist

| Check | Evidence |
| --- | --- |
| Python offline suite | Agent and clip tests pass. |
| Unity compile | No C# compiler errors in final project. |
| HTTP smoke | Live /state, /look, /depth, start, move and turn succeed. |
| Camera contract | Dimensions, FOV and camera pose verified. |
| Clock behavior | World pauses during model wait, advances during movement/turn. |
| Privacy boundary | Payload audit and trace show no engine-supplied object facts reach Claude. |
| Phase 1 completion | Three distinct unattended Claude passes and mentor sign-off. |
| Collision recovery | Visible stop/recovery and correct failure after three retries. |
| Phase 2 clip contract | 720p+, 10–20 seconds, overlay first two seconds, correct boundaries. |
| Feedback API | Official upload and pulse results retrieved after credentials issued. |
| Clean checkout | Fresh clone reproduces selected demo and includes attribution. |

## 8. Suggested implementation order

1. Choose final scene and lock camera/environment settings.
2. Implement and verify simulation pause/action clock.
3. Audit Claude boundary and image log completeness.
4. Run three unattended Claude tasks and collision recovery; obtain mentor sign-off.
5. Validate clip boundaries, overlay and format automatically.
6. Implement official Membrane client once guide and key are provided.
7. Execute hidden-command runbook.
8. Complete stretch goal; verify clean clone and asset attribution.

## 9. Out of scope for hackathon gates

The manufacturing pitch in README and PRD is a future direction. Fleet management, WMS/MES integration, site deployment, production safety certification, SKU recognition and commercial analytics are not hackathon acceptance gates; track them separately.


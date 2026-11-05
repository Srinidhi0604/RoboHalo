# RoboHalo — Additional Features and Optimisations

Status: items 1, 2 and 3 are built. The rest is backlog.
Related documents: `implementation_spec.md`, `spec_problem.md`, `README.md`

## 1. No API needed

| # | Feature | Why it helps | Effort |
| --- | --- | --- | --- |
| 1 (built) | **Run replay viewer**: an HTML page per run showing each step's frame, Claude's reasoning, the tool call and the result | Judges read the logs for 25% of the score, and this makes them easy to follow | 1–2 h |
| 2 (built) | **`/go_to {x, y}` endpoint**: the engine drives there using the existing A* planner | The brief allows it, and it means fewer steps and collisions and a more direct route, which is scored. It can't be combined with the "Claude plans with move and turn only" stretch goal. | 1 h |
| 3 (built) | **Randomised layout**: a seeded shuffle of object positions on reset | Shows nothing is hard-coded and stress-tests the agent before the hidden commands | 30 min |
| 4 | **Run statistics**: `python -m agent.stats` for pass rate, steps and cost across `runs/` | Tracks the $40 budget and the 120-run limit | 30 min |
| 5 | **Shortest order for Run ALL**: try all 720 orderings | Shorter total trip in the demo; this is the small version of the cuOpt idea | 30 min |
| 6 | **Infrared filter on the GPU** | Dark mode currently filters every pixel on the CPU, which may slow clip recording | 1 h |

### How to use the built items

- **Replay viewer:** every Claude run writes `replay.html` into its run folder. `python -m agent.replay runs/<folder>` rebuilds one, and `python -m agent.replay --all` rebuilds all.
- **`/go_to`:** `POST /go_to {"x": 8.1, "y": 5.2, "stop_short": 0.7}`. The agent has a matching `go_to` tool and now sees its own position each step. `python -m agent.run_agent "<command>" --no-go-to` runs with move and turn only, for the stretch goal.
- **Randomised layout:** `POST /reset {"seed": 7}` or the **SHUFFLE LAYOUT** panel button. The same seed always gives the same layout, and a plain reset restores the default.

- **Voice commands (built, added later):** `python -m agent.voice` listens for one spoken sentence with the offline Windows speech engine and hands it to the Claude agent as the command. `--yes` runs hands-free.

- **Quick mode (built, added later):** `python -m agent.quick "<request>"` uses one model call to turn a typed or spoken request into basic engine commands. It is fast, but the model is told what is in the scene, so it is for demos and tests only.

## 2. Need mentor approval (new sensors)

The brief's test for a new sensor: would a real robot have it, and does it stay silent about what an object is? Approved sensors are published to all teams.

| # | Feature | Why it helps | Effort |
| --- | --- | --- | --- |
| 7 | **Lidar-style 360° depth scan** (`/scan`) | Claude sees obstacles all round, not just straight ahead | 1 h |
| 8 | **Per-side bump sensors**: the collision result says front, left or right | Smarter recovery after a bump | 30 min |

## 3. Need the API key or Membrane guide

| # | Feature | Why it helps | Effort |
| --- | --- | --- | --- |
| 9 | **Rehearsal harness**: run a command list on Haiku, then Sonnet, and report pass rate, steps and cost | Finds prompt problems cheaply before judged runs | 1 h |
| 10 | **Multi-view memory**: Claude keeps short notes on what each view showed | Enables the stretch goal "the cube farthest from the zone" | 1–2 h |
| 11 | **Pulse feedback loop**: tune behaviour from crowd labels, such as "too slow" or "hit something" | This is the first stretch goal, with before and after clips | 2 h |
| 12 | **Dark-mode colour check**: briefly flash a white light to confirm colour before pickup | A strong product demo for lights-out warehouses | 1 h |

## 4. Recommended order

1. **1, 2 and 3** are built. They target the two biggest score parts: directness in the live demo and the log review.
2. Build **9** as soon as the API key is in.
3. Ask a mentor about **7** and **8** early, because approval takes up to 15 minutes and applies to every team.
4. Leave **10–12** until the Claude-driven runs pass.

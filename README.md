# RoboHalo

Unity engine prototype for the Membrane Robot Hackathon. The scene includes a
ROBOTIS TurtleBot3 Burger visual, five primitive objects, a drop zone, collision
handling, a forward camera, and the required local robot API. The product plan is
in [PRD.md](PRD.md).

## Open and run

1. Open `engine/` in Unity Hub. This project was created and built with
   **Unity 6000.6.3f1**, the editor installed on this machine. Use that exact
   version for now. The PRD's planned 6.3 LTS version would require opening and
   upgrading the project with a separate editor install.
2. Open `Assets/Scenes/RoboHalo.unity` and press Play. The engine listens at
   `http://localhost:8080/` while running.
3. For a lower-memory demo, use **RoboHalo > Build Windows Player** in Unity,
   close the Editor, and launch `engine/Build/RoboHalo.exe`.

The model is sourced from the official ROBOTIS TurtleBot3 repository. See the
[asset attribution](engine/Assets/Models/TurtleBot/ATTRIBUTION.md). The visual
mesh is separate from the simple physics collider to keep movement fast.

## Engine API

| Request | Result |
| --- | --- |
| `GET /state` | Robot state, x/y ground coordinates, heading, holding flag, retry count |
| `GET /look` | 640×480 robot-camera JPEG |
| `GET /depth` | Forward ray distance in metres or `null` |
| `POST /move {"distance":0.5}` | Move at 1 m/s; collision flag |
| `POST /turn {"degrees":30}` | Turn at 90 degrees/s |
| `POST /pickup` | Attach nearest object within 0.5 m and ±30 degrees |
| `POST /drop` | Release object; report whether it landed in the zone |

Example from PowerShell while the player is running:

```powershell
Invoke-RestMethod http://localhost:8080/state
Invoke-WebRequest http://localhost:8080/look -OutFile robot-view.jpg
Invoke-RestMethod http://localhost:8080/turn -Method Post -ContentType application/json -Body '{"degrees":30}'
Invoke-RestMethod http://localhost:8080/move -Method Post -ContentType application/json -Body '{"distance":0.3}'
```

Object details exist only inside the Unity scene. The public API does not send
object names, colors, shapes, counts or positions to the agent. The Python
Claude agent, JSONL logging, operator frontend and Phase 2 feedback upload
specified in the PRD are not yet implemented.

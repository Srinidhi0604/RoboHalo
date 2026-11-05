# TurtleBot3 arena engine (scripts)

Copies of the RoboHalo scripts from the TurtleBot3 Unity project, kept here so they are versioned.
The project itself lives at `E:\turtlebot3-unity-rossharp-main\turtlebot3-unity-rossharp-main` and is not in this repository.

| File | Goes in the Unity project at | What it does |
| --- | --- | --- |
| `Assets/Scripts/RobotEngine.cs` | `Assets/Scripts/RobotEngine.cs` | The robot engine: HTTP API on 127.0.0.1:8080 (8081 if taken), gripper, A* route planning, collisions, clip recorder, dark mode, camera views, the control panel with voice input. |
| `Assets/Editor/Round1SceneInstaller.cs` | `Assets/Editor/Round1SceneInstaller.cs` | Menu **RoboHalo > Install Round 1 Into Current Scene**: adds the engine object to `Tbot3SampleEnv`. |

These are snapshots: after changing the Unity project, copy the two files here again before committing.

using System;
using System.Collections;
using System.Collections.Concurrent;
using System.Collections.Generic;
using System.IO;
using System.Net;
using System.Text;
using System.Threading;
using UnityEngine;
using UnityEngine.SceneManagement;

public sealed class RobotEngine : MonoBehaviour
{
    [SerializeField] private GameObject warehouseRack;
    [SerializeField] private GameObject warehouseShelf;
    [SerializeField] private GameObject warehousePallet;
    [SerializeField] private GameObject warehouseParcel;
    // Tick to use the brief's plain four-walled room instead of the warehouse. The plain room is also
    // used automatically when the third-party Warehouse Pack is not installed (e.g. a clean clone).
    [SerializeField] private bool plainRoom;
    [Serializable] private sealed class MoveInput { public float distance; }
    [Serializable] private sealed class TurnInput { public float degrees; }
    [Serializable] private sealed class StartInput { public string command; public bool pickup_only; public string record_dir; public string score_as; }
    [Serializable] private sealed class ViewInput { public int index; }
    [Serializable] private sealed class ResetInput { public int seed; }
    [Serializable] private sealed class GoToInput { public float x; public float y; public float stop_short; }
    [Serializable] private sealed class GoToOutput { public bool ok; public bool collided; public bool reached; public float x; public float y; public float heading; }
    [Serializable] private sealed class SpeedInput { public float scale; }
    [Serializable] private sealed class SpeedOutput { public float scale; }
    [Serializable] private sealed class ObstacleInput { public bool on; }
    [Serializable] private sealed class ObstacleOutput { public bool on; }
    [Serializable] private sealed class ViewOutput { public int index; public string view; }
    [Serializable] private sealed class CommandOutput { public string command; public bool audio; }
    [Serializable] private sealed class StateOutput
    {
        public string name;
        public string state;
        public float x;
        public float y;
        public float heading;
        public bool holding;
        public int retry_count;
        public int retries;
        public string outcome;
    }
    [Serializable] private sealed class ActionOutput
    {
        public bool success;
        public bool collided;
        public bool in_zone;
        public bool ok;
        public float moved;
        public float heading;
    }

    private readonly ConcurrentQueue<HttpListenerContext> requests = new ConcurrentQueue<HttpListenerContext>();
    private HttpListener listener;
    private Thread listenerThread;
    private float nextListenerAttempt;
    private bool listenerErrorLogged;
    private int listenerPort = 8080;
    private Camera robotCamera;
    private Camera overviewCamera;
    private RenderTexture cameraTexture;
    private Texture2D cameraPixels;
    private GameObject sceneRobotVisualSource;
    private Transform robotRoot;
    private Transform held;
    private Transform[] objects;
    private readonly HashSet<string> deliveredTargets = new HashSet<string>(StringComparer.Ordinal);
    private Vector3 currentDropPosition;
    private Vector3 layoutCenter;
    private float layoutHalfX;
    private float layoutRowZ;
    private float layoutEndZ;
    private float layoutFarZ; // row of objects beyond the robot start, far from the drop zone
    // These are centered and spread across the available warehouse floor in BuildScene.
    private Vector3 robotStart;
    private Vector3 zoneCenter;
    private float floorHeight;
    private string robotState = "Idle";
    private float heading;
    private int retries;
    private string targetName;
    private string outcome;
    private bool busy;
    private bool lastCollided;
    private float lastMoved;
    private string guiCommand = "Bring the round red thing";
    private string pendingAgentCommand;
    // The Claude agent that serves the AI COMMAND box. Send and Speak start it if nothing is polling.
    public string agentFolder = @"E:\ansu-hack";
    public string pythonCommand = "python";
    private System.Diagnostics.Process agentProcess;
    private float lastAgentPoll = -10f;
    private string agentNote = "";

    private void EnsureAgent()
    {
        if (Time.realtimeSinceStartup - lastAgentPoll < 3f) return;      // a listener is already polling
        if (agentProcess != null && !agentProcess.HasExited) return;     // ours is still starting up
        try
        {
            agentProcess = System.Diagnostics.Process.Start(new System.Diagnostics.ProcessStartInfo
            {
                FileName = pythonCommand,
                Arguments = "-m agent.run_agent --listen --port " + listenerPort,
                WorkingDirectory = agentFolder,
                UseShellExecute = false,
                CreateNoWindow = true,
            });
            agentNote = "";
        }
        catch (Exception error) { agentNote = "Could not start the agent: " + error.Message; }
    }

    private bool listening;
    private string voiceNote = "";
    private AudioClip voiceClip;
    private string voiceDevice;
    private float voiceStarted, voiceLastSound, voicePeak;
    private bool voiceHeardSpeech;
    private byte[] pendingAudio; // finished recording, waiting for the agent process to transcribe it
    private const int VoiceRate = 16000, VoiceMaxSeconds = 10;

    // Voice command: Unity records the microphone itself; the agent process (python -m agent.run_agent --listen)
    // fetches the recording, transcribes it with Whisper, and runs it as if it had been typed.
    // Recording stops on its own after a pause in speech, after 10 s, or when Stop is pressed.
    private void StartListening()
    {
        if (Microphone.devices.Length == 0) { StopListening("No microphone found. Plug one in or enable it in Windows Sound settings."); return; }
        voiceDevice = Microphone.devices[0];
        voiceClip = Microphone.Start(voiceDevice, false, VoiceMaxSeconds, VoiceRate);
        if (voiceClip == null) { StopListening("Could not open the microphone (" + voiceDevice + ")."); return; }
        listening = true;
        voiceStarted = voiceLastSound = Time.realtimeSinceStartup;
        voicePeak = 0f;
        voiceHeardSpeech = false;
        guiStatus = voiceNote = "Recording... say the command (" + voiceDevice + ").";
    }

    // Called every frame while recording: watch the level to stop after the speaker pauses.
    private void VoiceTick()
    {
        if (!listening || voiceClip == null) return;
        int position = Microphone.GetPosition(voiceDevice);
        float now = Time.realtimeSinceStartup;
        if (position > 1600)
        {
            var window = new float[1600]; // last 0.1 s
            voiceClip.GetData(window, position - window.Length);
            float level = 0f;
            foreach (float v in window) level = Mathf.Max(level, Mathf.Abs(v));
            voicePeak = Mathf.Max(voicePeak, level);
            if (level > 0.04f) { voiceHeardSpeech = true; voiceLastSound = now; }
        }
        bool pausedAfterSpeech = voiceHeardSpeech && now - voiceLastSound > 1.3f;
        bool nothingSaid = !voiceHeardSpeech && now - voiceStarted > 6f;
        if (pausedAfterSpeech || nothingSaid || !Microphone.IsRecording(voiceDevice) || now - voiceStarted > VoiceMaxSeconds) FinishRecording();
    }

    private void FinishRecording()
    {
        int length = Microphone.GetPosition(voiceDevice);
        if (!Microphone.IsRecording(voiceDevice)) length = voiceClip.samples; // ran to the 10 s limit
        Microphone.End(voiceDevice);
        if (voicePeak < 0.005f) { StopListening("The microphone gave only silence. Check it is unmuted (often F4) and selected in Windows Sound > Input."); return; }
        if (!voiceHeardSpeech || length < VoiceRate / 2) { StopListening("Heard nothing loud enough. Press Speak and talk a little closer to the microphone."); return; }
        var samples = new float[length];
        voiceClip.GetData(samples, 0);
        pendingAudio = Wav(samples, VoiceRate);
        StopListening("Recorded " + (length / (float)VoiceRate).ToString("0.0") + " s. Transcribing...");
    }

    private static byte[] Wav(float[] samples, int rate) // 16-bit mono PCM
    {
        using (var stream = new MemoryStream())
        using (var w = new BinaryWriter(stream))
        {
            w.Write(Encoding.ASCII.GetBytes("RIFF")); w.Write(36 + samples.Length * 2); w.Write(Encoding.ASCII.GetBytes("WAVE"));
            w.Write(Encoding.ASCII.GetBytes("fmt ")); w.Write(16); w.Write((short)1); w.Write((short)1); w.Write(rate); w.Write(rate * 2); w.Write((short)2); w.Write((short)16);
            w.Write(Encoding.ASCII.GetBytes("data")); w.Write(samples.Length * 2);
            foreach (float v in samples) w.Write((short)Mathf.Clamp(v * 32767f, -32768f, 32767f));
            return stream.ToArray();
        }
    }

    private void StopListening(string status)
    {
        if (listening && voiceDevice != null && Microphone.IsRecording(voiceDevice)) Microphone.End(voiceDevice);
        listening = false;
        guiStatus = status;
        if (voiceNote != status) Debug.Log("Voice command: " + status); // also in the Console, where it cannot be cut off
        voiceNote = status;
    }
    private string guiStatus = "Idle. Ready for command.";
    private Vector3[] spawnPositions;
    private Quaternion[] spawnRotations;
    // Match the visible chassis width and leave a safe gap to the larger Phase 1 shapes.
    private Bounds planBounds;
    private bool running;
    private static readonly string[] shapeCommands = {
        "Bring the red sphere", "Bring the red cube", "Bring the blue sphere",
        "Bring the blue cube", "Bring the yellow cylinder", "Bring the yellow cone" };
    // Held objects sit this far in front of the robot centre, between the gripper fingers,
    // and are set down exactly there (robot radius 0.4 + largest object half-width 0.29 < 0.7).
    private const float HoldDistance = 0.7f;
    private const float DropDistance = HoldDistance;
    private const float GripOpen = 0.36f;
    // A carried item is lifted so its underside is this high: above the camera's view, forklift style.
    // Held at floor level it sat right in front of the lens and hid the floor and the drop zone.
    private const float CarryHeight = 0.9f, GripRest = 0.14f;
    private const float MastTop = 1.5f; // gripper masts
    // Highest point of the robot and its load: the masts, or the top of a lifted item.
    private float RobotTop => held != null ? Mathf.Max(MastTop, 2f * holdPose.y - CarryHeight) : MastTop;
    private float gripLift = GripRest;
    private Transform gripCarriage;
    private Transform leftFinger, rightFinger;
    private float gripWidth = GripOpen, gripClosed = GripOpen;
    private Vector3 holdPose;
    private const float Radius = 0.4f;
    private const float Reach = 0.86f;

    private void Start()
    {
        StartCoroutine(StartWarehouse());
    }

    private IEnumerator StartWarehouse()
    {
        Application.runInBackground = true;
        QualitySettings.vSyncCount = 0;
        Application.targetFrameRate = 60;
        sceneRobotVisualSource = GameObject.Find("turtlebot3_burger");
        DisableOtherSceneRoots();
        if (!plainRoom && WarehouseAvailable()) yield return SceneManager.LoadSceneAsync("URPScene", LoadSceneMode.Additive);
        else BuildPlainRoom();
        BuildScene();
        RemoveDecorativeWarehouseBlockers();
        ClearRoundOnePlayArea();
        MatchWarehouseColliders();
        StartListener();
        sceneReady = true;
        ShowInSceneView();
    }

    // While playing in the editor, point the Scene view at the same arena the Game view shows and
    // select the robot, so the Hierarchy opens on the live objects (robot, gripper, items, drop zone,
    // cameras, warehouse). Editor only; it changes nothing about the run.
    private void ShowInSceneView()
    {
#if UNITY_EDITOR
        UnityEditor.EditorApplication.delayCall += () =>
        {
            if (this == null || robotRoot == null) return;
            UnityEditor.Selection.activeGameObject = robotRoot.gameObject;
            foreach (UnityEditor.SceneView view in UnityEditor.SceneView.sceneViews)
            {
                if (overviewCamera != null) view.AlignViewToObject(overviewCamera.transform); // same framing as the Game overview
                else view.LookAt(layoutCenter + Vector3.up, Quaternion.Euler(40f, 30f, 0f), 8f, false, true);
                view.Repaint();
            }
        };
#endif
    }

    private bool sceneReady;

    private void StartListener()
    {
        foreach (int port in new[] { 8080, 8081 })
        {
            listener = new HttpListener();
            listener.Prefixes.Add("http://127.0.0.1:" + port + "/");
            try
            {
                listener.Start();
                listenerPort = port;
                listenerThread = new Thread(Listen) { IsBackground = true };
                listenerThread.Start();
                listenerErrorLogged = false;
                Debug.Log("RoboHalo engine listening on http://127.0.0.1:" + port + "/");
                if (port != 8080) Debug.LogWarning("Port 8080 belongs to another Unity project. The agent listener needs --port " + port + ".");
                return;
            }
            catch (Exception)
            {
                listener.Close();
                listener = null;
                listenerThread = null;
            }
        }
        if (!listenerErrorLogged) Debug.LogError("RoboHalo could not start HTTP server on ports 8080 or 8081. Stop the other servers and Play will retry automatically.");
        listenerErrorLogged = true;
    }

    private void Listen()
    {
        while (listener != null && listener.IsListening)
        {
            try { requests.Enqueue(listener.GetContext()); }
            catch (HttpListenerException) { break; }
            catch (ObjectDisposedException) { break; }
        }
    }

    private static bool WarehouseAvailable()
    {
#if UNITY_EDITOR
        if (!File.Exists("Assets/WarehousePack/Samples/URP/URPScene.unity")) return false;
#endif
        return Application.CanStreamedLevelBeLoaded("URPScene");
    }

    // The brief's scene: one flat room, four walls, no furniture (8 x 11 m).
    private void BuildPlainRoom()
    {
        var center = new Vector3(8.1f, 0f, 5.4f);
        const float width = 8f, depth = 11f, height = 2.5f, thick = 0.2f;
        var wallColor = new Color(0.86f, 0.86f, 0.84f);
        Box("Floor", center + Vector3.down * 0.05f, new Vector3(width, 0.1f, depth), new Color(0.62f, 0.62f, 0.60f));
        Box("Wall_North", center + new Vector3(0f, height / 2f, depth / 2f + thick / 2f), new Vector3(width + 2 * thick, height, thick), wallColor);
        Box("Wall_South", center + new Vector3(0f, height / 2f, -depth / 2f - thick / 2f), new Vector3(width + 2 * thick, height, thick), wallColor);
        Box("Wall_East", center + new Vector3(width / 2f + thick / 2f, height / 2f, 0f), new Vector3(thick, height, depth), wallColor);
        Box("Wall_West", center + new Vector3(-width / 2f - thick / 2f, height / 2f, 0f), new Vector3(thick, height, depth), wallColor);
        RenderSettings.ambientMode = UnityEngine.Rendering.AmbientMode.Flat;
        RenderSettings.ambientLight = new Color(0.45f, 0.45f, 0.47f);
    }

    private void DisableOtherSceneRoots()
    {
        foreach (var root in SceneManager.GetActiveScene().GetRootGameObjects())
        {
            if (root == gameObject) continue;
            root.SetActive(false);
        }
    }

    private void Update()
    {
        VoiceTick();
        // A script reload during Play kills the listener thread; bring the server back.
        if (sceneReady && Time.realtimeSinceStartup >= nextListenerAttempt && (listener == null || !listener.IsListening || listenerThread == null || !listenerThread.IsAlive))
        {
            try { listener?.Close(); } catch (Exception) { }
            busy = false; driving = false;
            nextListenerAttempt = Time.realtimeSinceStartup + 3f;
            StartListener();
        }
        // /state, /look, /depth (GET) are answered even mid-move; POST actions wait until the robot is idle.
        for (int pending = requests.Count; pending > 0 && requests.TryDequeue(out var request); pending--)
        {
            if ((busy || driving) && request.Request.HttpMethod != "GET") requests.Enqueue(request);
            else Handle(request);
        }
        AnimateGripper();
    }

    // Parallel gripper: fingers slide shut on a held object and open when it is released.
    private void AnimateGripper()
    {
        if (leftFinger == null) return;
        gripWidth = Mathf.MoveTowards(gripWidth, held != null ? gripClosed : GripOpen, Time.deltaTime * 1.2f);
        gripLift = Mathf.MoveTowards(gripLift, held != null ? holdPose.y : GripRest, Time.deltaTime * 1.5f);
        leftFinger.localPosition = new Vector3(-gripWidth, gripLift, HoldDistance);
        rightFinger.localPosition = new Vector3(gripWidth, gripLift, HoldDistance);
        if (gripCarriage != null) gripCarriage.localPosition = new Vector3(0f, gripLift, 0.44f);
        if (held != null) held.localPosition = Vector3.MoveTowards(held.localPosition, holdPose, Time.deltaTime * 1.5f);
    }

    // ---- Display cameras (what the screen shows; /look always uses the robot camera) ----
    private static readonly float[] speeds = { 1f, 2f, 4f };

    // Stretch goal: a wall across the straight line from the robot start to the drop zone, so the
    // robot has to find a detour. Operator toggle (panel button or /obstacle); the planner treats it
    // like any other prop, and Claude would see it only as pixels and a depth reading.
    private Transform obstacle;

    // ---- Dark mode (demo only): room lights off, robot sees with an infrared illuminator ----
    // A near-IR camera records brightness, not colour, so frames from /look and the clip become
    // monochrome and coloured objects of the same material look alike. Shape and size still show.
    // The /look contract (640x480 JPEG, same pose and FOV) is unchanged. Not for judged runs.
    private bool darkMode;
    private Light irLight;
    private readonly Dictionary<Light, bool> savedLights = new Dictionary<Light, bool>();
    private UnityEngine.Rendering.AmbientMode savedAmbientMode;
    private Color savedAmbientLight, savedAmbientSky, savedAmbientEquator, savedAmbientGround;
    private float savedAmbientIntensity, savedReflectionIntensity;
    private LightmapData[] savedLightmaps;
    private LightProbes savedLightProbes;
    private uint irNoise = 12345u;

    private void SetDarkMode(bool on)
    {
        if (on == darkMode) return;
        darkMode = on;
        if (irLight == null && robotRoot != null && robotCamera != null)
        {
            var go = new GameObject("IR Illuminator");
            go.transform.SetParent(robotRoot, false);
            go.transform.localPosition = robotCamera.transform.localPosition;
            go.transform.localRotation = robotCamera.transform.localRotation;
            irLight = go.AddComponent<Light>();
            irLight.type = LightType.Spot;
            irLight.range = 9f;
            irLight.spotAngle = 115f;
            irLight.innerSpotAngle = 60f;
            irLight.intensity = 3.5f;
            irLight.color = Color.white;
            irLight.shadows = LightShadows.Soft;
            irLight.enabled = false;
        }
        if (on)
        {
            savedLights.Clear();
            foreach (var light in FindObjectsByType<Light>())
                if (light != irLight) { savedLights[light] = light.enabled; light.enabled = false; }
            savedAmbientMode = RenderSettings.ambientMode;
            savedAmbientLight = RenderSettings.ambientLight;
            savedAmbientSky = RenderSettings.ambientSkyColor;
            savedAmbientEquator = RenderSettings.ambientEquatorColor;
            savedAmbientGround = RenderSettings.ambientGroundColor;
            savedAmbientIntensity = RenderSettings.ambientIntensity;
            savedReflectionIntensity = RenderSettings.reflectionIntensity;
            savedLightmaps = LightmapSettings.lightmaps;
            savedLightProbes = LightmapSettings.lightProbes;
            RenderSettings.ambientMode = UnityEngine.Rendering.AmbientMode.Flat;
            RenderSettings.ambientLight = new Color(0.015f, 0.015f, 0.02f);
            RenderSettings.ambientIntensity = 0f;
            RenderSettings.reflectionIntensity = 0f;
            LightmapSettings.lightmaps = new LightmapData[0];   // baked warehouse lighting off too
            LightmapSettings.lightProbes = null;
        }
        else
        {
            foreach (var pair in savedLights) if (pair.Key != null) pair.Key.enabled = pair.Value;
            savedLights.Clear();
            RenderSettings.ambientMode = savedAmbientMode;
            RenderSettings.ambientLight = savedAmbientLight;
            RenderSettings.ambientSkyColor = savedAmbientSky;
            RenderSettings.ambientEquatorColor = savedAmbientEquator;
            RenderSettings.ambientGroundColor = savedAmbientGround;
            RenderSettings.ambientIntensity = savedAmbientIntensity;
            RenderSettings.reflectionIntensity = savedReflectionIntensity;
            if (savedLightmaps != null) LightmapSettings.lightmaps = savedLightmaps;
            LightmapSettings.lightProbes = savedLightProbes;
        }
        DynamicGI.UpdateEnvironment(); // apply the ambient change now, not a few frames later
        if (irLight != null) irLight.enabled = on;
        guiStatus = on ? "Dark mode: room lights off, robot camera on infrared." : "Dark mode off: room lights on.";
    }

    // Near-IR look: brightness without hue. Equal-brightness red, blue and yellow paint end up the
    // same grey, plus a little sensor noise.
    private void ApplyInfrared(Texture2D texture)
    {
        var px = texture.GetPixels32();
        for (int i = 0; i < px.Length; i++)
        {
            var c = px[i];
            int max = Mathf.Max(c.r, Mathf.Max(c.g, c.b));
            int avg = (c.r + c.g + c.b) / 3;
            irNoise = irNoise * 1664525u + 1013904223u;
            int noise = (int)(irNoise >> 28) - 8;          // -8..7
            byte v = (byte)Mathf.Clamp((max + avg) / 2 + noise, 0, 255);
            px[i] = new Color32(v, v, v, 255);
        }
        texture.SetPixels32(px);
        texture.Apply();
    }

    private Texture2D irDisplay;
    private float nextIrDisplay;

    private void RefreshInfraredDisplay()
    {
        if (!darkMode || viewIndex != 3 || robotCamera == null || Time.unscaledTime < nextIrDisplay) return;
        nextIrDisplay = Time.unscaledTime + 0.066f; // ~15 fps is plenty for the operator view
        if (irDisplay == null) irDisplay = new Texture2D(640, 480, TextureFormat.RGB24, false);
        robotCamera.Render();
        RenderTexture.active = cameraTexture;
        irDisplay.ReadPixels(new Rect(0, 0, 640, 480), 0, 0);
        RenderTexture.active = null;
        ApplyInfrared(irDisplay);
    }

    private void SetObstacle(bool on)
    {
        if (on && obstacle == null)
        {
            var wall = Box("Detour wall", new Vector3(layoutCenter.x, floorHeight + 0.4f, layoutCenter.z), new Vector3(2.4f, 0.8f, 0.25f), new Color(0.35f, 0.38f, 0.45f));
            obstacle = wall.transform;
        }
        if (obstacle != null) obstacle.gameObject.SetActive(on);
        guiStatus = on ? "Detour wall placed between the robot and the drop zone." : "Detour wall removed.";
    }

    // Scales all motion and animation. 1x = the documented 1 m/s and 90 deg/s; use 1x for judged clips.
    private void SetSpeed(float speed) { Time.timeScale = speed; }

    private static readonly string[] viewNames = { "Overview", "Follow", "Top-down", "Robot POV" };
    private Camera[] viewCameras;
    private int viewIndex;

    private void SetView(int index)
    {
        if (viewCameras == null || viewCameras.Length == 0) return;
        viewIndex = (index + viewCameras.Length) % viewCameras.Length;
        for (int i = 0; i < viewCameras.Length; i++)
            if (viewCameras[i] != null)
            {
                viewCameras[i].enabled = i == viewIndex;
                viewCameras[i].targetTexture = null;
                viewCameras[i].depth = i == viewIndex ? 50f : 0f; // the chosen view always draws last, on top
            }
        guiStatus = "Camera: " + viewNames[viewIndex];
    }

    private void LateUpdate()
    {
        RecordFrame();
        RefreshInfraredDisplay();
        // The control panel is a sidebar: the display camera draws only in the area to its right,
        // so the panel never covers part of the scene.
        if (viewCameras != null && viewIndex < viewCameras.Length && viewCameras[viewIndex] != null)
        {
            float side = PanelPixels / Mathf.Max(1f, Screen.width);
            viewCameras[viewIndex].rect = new Rect(side, 0f, 1f - side, 1f);
            if (viewIndex == 2) // top-down: zoom out as far as needed to keep the whole arena in a narrow view
                viewCameras[2].orthographicSize = Mathf.Max((layoutHalfX + 1.5f) * 1.1f, (Mathf.Max(layoutEndZ, layoutFarZ) + 1.2f) / Mathf.Max(0.2f, viewCameras[2].aspect));
        }
        if (viewCameras == null || viewCameras.Length < 2 || viewCameras[1] == null || robotRoot == null) return;
        // Follow camera: behind and above the robot, eased so turns look smooth.
        var follow = viewCameras[1].transform;
        Vector3 wantPos = robotRoot.position - Forward() * 2.2f + Vector3.up * 1.6f;
        follow.position = Vector3.Lerp(follow.position, wantPos, 1f - Mathf.Exp(-6f * Time.deltaTime));
        follow.rotation = Quaternion.Slerp(follow.rotation, Quaternion.LookRotation(robotRoot.position + Forward() * 0.8f + Vector3.up * 0.2f - follow.position), 1f - Mathf.Exp(-8f * Time.deltaTime));
    }

    // ---- Phase 2 clip recorder: robot POV at 1280x720, frames only while the scene advances ----
    // The engine clock is frozen while Claude thinks, so recording only during motion (plus short holds
    // to show pickup/drop/bumps) makes the clip length equal to real robot motion time.
    private const int ClipWidth = 1280, ClipHeight = 720, ClipFps = 30;
    private Camera clipCamera;
    private RenderTexture clipTexture;
    private Texture2D clipPixels;
    private string recordDir;
    private int recordFrames;
    private float recordClock, recordHold;
    private bool recording;
    [Serializable] private sealed class RecordOutput { public bool recording; public int frames; public int fps; public int width; public int height; }

    private void StartRecording(string dir)
    {
        if (string.IsNullOrEmpty(dir)) { recording = false; return; }
        Directory.CreateDirectory(dir);
        recordDir = dir; recordFrames = 0; recordClock = 0f; recordHold = 0.3f; recording = true;
    }

    private void RecordFrame()
    {
        if (!recording || clipCamera == null) return;
        if (!(busy || recordHold > 0f))
        {
            if (robotState == "Done") recording = false; // final hold finished
            return;
        }
        recordHold = Mathf.Max(0f, recordHold - Time.deltaTime);
        recordClock += Time.deltaTime;
        int due = Mathf.FloorToInt(recordClock * ClipFps) - recordFrames;
        if (due <= 0) return;
        clipCamera.Render();
        RenderTexture.active = clipTexture;
        clipPixels.ReadPixels(new Rect(0, 0, ClipWidth, ClipHeight), 0, 0);
        clipPixels.Apply();
        if (darkMode) ApplyInfrared(clipPixels);
        RenderTexture.active = null;
        byte[] jpg = clipPixels.EncodeToJPG(90);
        for (int i = 0; i < due; i++) File.WriteAllBytes(Path.Combine(recordDir, "frame_" + (recordFrames++).ToString("D5") + ".jpg"), jpg);
    }

    private void BuildViewCameras()
    {
        Camera Make(string name)
        {
            var cam = new GameObject(name).AddComponent<Camera>();
            cam.nearClipPlane = 0.05f; cam.farClipPlane = 100f; cam.enabled = false;
            return cam;
        }
        var followCam = Make("View Follow Camera");
        followCam.fieldOfView = 60f;
        followCam.transform.position = robotRoot.position - Forward() * 2.2f + Vector3.up * 1.6f;
        followCam.transform.LookAt(robotRoot.position);

        var topCam = Make("View Top-down Camera");
        topCam.orthographic = true;
        topCam.orthographicSize = (layoutHalfX + 1.5f) * 1.1f;
        topCam.transform.position = new Vector3(layoutCenter.x, floorHeight + 12f, layoutCenter.z); // centred: the sidebar no longer overlaps the view
        // The warehouse has a roof and overhead props: only draw what is within 1.5 m of the floor
        // (the tallest task object is under 1 m), so the camera looks straight into the room.
        topCam.nearClipPlane = 12f - 1.5f;
        topCam.farClipPlane = 12.5f;
        topCam.transform.rotation = Quaternion.Euler(90f, 90f, 0f); // long axis of the arena runs left-right on screen

        // Same pose as the /look camera but drawn to the screen, so the operator sees what Claude sees.
        var povCam = Make("View Robot POV Camera");
        povCam.transform.SetParent(robotRoot, false);
        povCam.transform.localPosition = robotCamera.transform.localPosition;
        povCam.transform.localRotation = robotCamera.transform.localRotation;
        povCam.fieldOfView = robotCamera.fieldOfView;
        povCam.nearClipPlane = robotCamera.nearClipPlane;

        // Recording camera: same mount as the robot camera, 16:9 with the same 90 deg horizontal FOV.
        clipCamera = Make("Clip Recording Camera");
        clipCamera.transform.SetParent(robotRoot, false);
        clipCamera.transform.localPosition = robotCamera.transform.localPosition;
        clipCamera.transform.localRotation = robotCamera.transform.localRotation;
        clipCamera.aspect = 16f / 9f;
        clipCamera.fieldOfView = 2f * Mathf.Atan(Mathf.Tan(45f * Mathf.Deg2Rad) / clipCamera.aspect) * Mathf.Rad2Deg;
        clipCamera.nearClipPlane = robotCamera.nearClipPlane;
        clipTexture = new RenderTexture(ClipWidth, ClipHeight, 24);
        clipPixels = new Texture2D(ClipWidth, ClipHeight, TextureFormat.RGB24, false);
        clipCamera.targetTexture = clipTexture;

        viewCameras = new[] { overviewCamera, followCam, topCam, povCam };
        SetView(0);
    }

    private void BuildGripper(Transform parent)
    {
        var dark = new Color(0.18f, 0.19f, 0.22f);
        var accent = new Color(1f, 0.55f, 0.10f);
        GameObject Part(string name, Vector3 localPos, Vector3 scale, Color color)
        {
            var part = Box(name, Vector3.zero, scale, color);
            Destroy(part.GetComponent<Collider>()); // visual only; movement checks cover the gripper
            part.transform.SetParent(parent, false);
            part.transform.localPosition = localPos;
            return part;
        }
        Part("Gripper arm", new Vector3(0f, 0.16f, 0.36f), new Vector3(0.12f, 0.06f, 0.14f), dark);
        // Two side masts (outside the camera's 90 degree view) and a carriage that rides up them with the fingers.
        Part("Gripper mast L", new Vector3(-0.38f, 0.82f, 0.44f), new Vector3(0.05f, 1.36f, 0.05f), dark);
        Part("Gripper mast R", new Vector3(0.38f, 0.82f, 0.44f), new Vector3(0.05f, 1.36f, 0.05f), dark);
        gripCarriage = Part("Gripper carriage", new Vector3(0f, GripRest, 0.44f), new Vector3(0.80f, 0.10f, 0.06f), dark).transform;
        leftFinger = Part("Gripper finger L", new Vector3(-GripOpen, 0.14f, HoldDistance), new Vector3(0.04f, 0.16f, 0.52f), accent).transform;
        rightFinger = Part("Gripper finger R", new Vector3(GripOpen, 0.14f, HoldDistance), new Vector3(0.04f, 0.16f, 0.52f), accent).transform;
    }

    // The gripper sticks out ahead of the body: stop forward moves before it hits walls or props.
    // Task objects are ignored here because the open fingers straddle them to pick up.
    // Gripper/held-object contact rule: walls, props and delivered objects always block. Loose task
    // objects are ignored only while the gripper is empty, because the open fingers straddle them to grab.
    private bool BlocksGripper(Collider col)
    {
        if (col == null || col.transform == robotRoot || col.transform.IsChildOf(robotRoot)) return false;
        if (col.bounds.max.y <= floorHeight + 0.08f) return false;
        if (held == null && Array.IndexOf(objects, col.transform) >= 0 && !deliveredTargets.Contains(col.transform.name)) return false;
        return true;
    }

    private float GripperRadius => held != null ? Mathf.Max(0.2f, gripClosed) : 0.2f;

    // Is the gripper (and anything it holds) touching something when the robot faces headingDeg?
    private bool GripperOverlaps(float headingDeg, out Vector3 point)
    {
        point = default;
        var f = new Vector3(Mathf.Cos(headingDeg * Mathf.Deg2Rad), 0f, -Mathf.Sin(headingDeg * Mathf.Deg2Rad));
        Vector3 at = robotRoot.position + f * HoldDistance;
        Vector3 low = at + Vector3.up * 0.14f, high = at + Vector3.up * Mathf.Max(0.14f, RobotTop - GripperRadius);
        foreach (var col in Physics.OverlapCapsule(low, high, GripperRadius, ~0, QueryTriggerInteraction.Ignore))
        {
            if (!BlocksGripper(col)) continue;
            point = col.ClosestPoint((low + high) / 2f);
            return true;
        }
        return false;
    }

    // Anything the gripper could hit within `margin` of it at the current heading?
    private bool GripperNear(float margin)
    {
        Vector3 at = robotRoot.position + Forward() * HoldDistance;
        Vector3 low = at + Vector3.up * 0.14f, high = at + Vector3.up * Mathf.Max(0.14f, RobotTop - GripperRadius);
        foreach (var col in Physics.OverlapCapsule(low, high, GripperRadius + margin, ~0, QueryTriggerInteraction.Ignore))
            if (BlocksGripper(col)) return true;
        return false;
    }

    private bool GripperBlocked(Vector3 direction, float distance, out Vector3 point)
    {
        point = default;
        Vector3 at = robotRoot.position + direction * (HoldDistance - 0.1f);
        Vector3 low = at + Vector3.up * 0.14f, high = at + Vector3.up * Mathf.Max(0.14f, RobotTop - GripperRadius);
        foreach (var hit in Physics.CapsuleCastAll(low, high, GripperRadius, direction, distance, ~0, QueryTriggerInteraction.Ignore))
        {
            if (!BlocksGripper(hit.collider)) continue;
            point = hit.distance > 0f ? hit.point : (low + high) / 2f;
            return true;
        }
        return false;
    }

    // ---------------- Control panel (IMGUI) ----------------
    private static readonly Color PanelColor = new Color(0.07f, 0.08f, 0.11f, 0.94f);
    private static readonly Color CardColor = new Color(0.13f, 0.15f, 0.20f, 1f);
    private static readonly Color AccentColor = new Color(0.25f, 0.62f, 1f);
    private readonly Dictionary<Color, Texture2D> solidTextures = new Dictionary<Color, Texture2D>();
    private readonly Dictionary<string, GUIStyle> styles = new Dictionary<string, GUIStyle>();

    private Texture2D Solid(Color c)
    {
        if (!solidTextures.TryGetValue(c, out var t) || t == null)
        {
            t = new Texture2D(1, 1) { hideFlags = HideFlags.HideAndDontSave };
            t.SetPixel(0, 0, c); t.Apply();
            solidTextures[c] = t;
        }
        return t;
    }

    private GUIStyle ButtonStyle(Color fill, Color text, int size = 13)
    {
        string key = "b" + fill + text + size;
        if (styles.TryGetValue(key, out var existing)) return existing;
        var st = new GUIStyle(GUI.skin.button) { fontSize = size, fontStyle = FontStyle.Bold, alignment = TextAnchor.MiddleCenter, border = new RectOffset(0, 0, 0, 0) };
        st.normal.background = Solid(fill); st.normal.textColor = text;
        st.hover.background = Solid(Color.Lerp(fill, Color.white, 0.18f)); st.hover.textColor = text;
        st.active.background = Solid(Color.Lerp(fill, Color.black, 0.25f)); st.active.textColor = text;
        st.onNormal = st.normal;
        styles[key] = st;
        return st;
    }

    private GUIStyle TextStyle(int size, Color color, FontStyle fontStyle = FontStyle.Normal, TextAnchor anchor = TextAnchor.MiddleLeft)
    {
        string key = "t" + size + color + fontStyle + anchor;
        if (styles.TryGetValue(key, out var existing)) return existing;
        var st = new GUIStyle(GUI.skin.label) { fontSize = size, fontStyle = fontStyle, alignment = anchor, wordWrap = true, richText = true };
        st.normal.textColor = color;
        styles[key] = st;
        return st;
    }

    private GUIStyle FillStyle(Color fill)
    {
        string key = "f" + fill;
        if (styles.TryGetValue(key, out var existing)) return existing;
        var st = new GUIStyle(GUI.skin.box) { border = new RectOffset(0, 0, 0, 0), margin = new RectOffset(0, 0, 0, 0), padding = new RectOffset(12, 12, 10, 10) };
        st.normal.background = Solid(fill);
        styles[key] = st;
        return st;
    }

    private static Color StateColor(string state)
    {
        switch (state)
        {
            case "Done": return new Color(0.30f, 0.80f, 0.45f);
            case "Blocked": return new Color(1f, 0.38f, 0.35f);
            case "Idle": return new Color(0.55f, 0.60f, 0.68f);
            case "Carry": case "Drop": return new Color(1f, 0.72f, 0.25f);
            default: return new Color(0.25f, 0.62f, 1f);
        }
    }

    private static Color ObjectColor(string command)
    {
        if (command.Contains("red")) return new Color(0.86f, 0.24f, 0.24f);
        if (command.Contains("blue")) return new Color(0.22f, 0.42f, 0.92f);
        return new Color(0.95f, 0.76f, 0.15f);
    }

    private void OnGUI()
    {
        // C cycles the camera (IMGUI events work with the Input System; ignored while typing a command).
        if (Event.current.type == EventType.KeyDown && Event.current.keyCode == KeyCode.C && GUIUtility.keyboardControl == 0)
        { SetView(viewIndex + 1); Event.current.Use(); }
        // Dark mode + Robot POV: show the operator the same infrared image Claude gets.
        if (darkMode && viewIndex == 3 && irDisplay != null && Event.current.type == EventType.Repaint)
            GUI.DrawTexture(new Rect(PanelPixels, 0, Screen.width - PanelPixels, Screen.height), irDisplay, ScaleMode.ScaleAndCrop);
        if (Event.current.type == EventType.KeyDown && Event.current.keyCode == KeyCode.H && GUIUtility.keyboardControl == 0)
        { panelHidden = !panelHidden; Event.current.Use(); }
        float scale = Mathf.Max(1f, Screen.height / 900f);
        GUIUtility.ScaleAroundPivot(new Vector2(scale, scale), Vector2.zero);
        const float width = 360f;
        if (!panelHidden) GUI.DrawTexture(new Rect(0, 0, 388f, Screen.height / scale + 1f), Solid(new Color(0.07f, 0.08f, 0.11f, 1f))); // sidebar background, full height
        GUILayout.BeginArea(new Rect(14, 14, width, 2000));
        if (panelHidden)
        {
            if (GUILayout.Button("ROBOHALO   Show panel (H)", ButtonStyle(PanelColor, Color.white, 12), GUILayout.Width(210), GUILayout.Height(30))) panelHidden = false;
            GUILayout.EndArea();
            return;
        }
        GUILayout.BeginVertical(FillStyle(PanelColor)); // background wraps the content: no empty tail

        // Header
        GUILayout.BeginHorizontal();
        GUILayout.Label("ROBOHALO", TextStyle(22, Color.white, FontStyle.Bold));
        GUILayout.FlexibleSpace();
        GUILayout.Label("FETCH ARENA", TextStyle(11, AccentColor, FontStyle.Bold, TextAnchor.MiddleRight));
        GUILayout.Space(8);
        if (GUILayout.Button("Hide (H)", ButtonStyle(CardColor, new Color(0.75f, 0.79f, 0.86f), 10), GUILayout.Width(64), GUILayout.Height(22))) panelHidden = true;
        GUILayout.EndHorizontal();
        GUILayout.Space(6);

        // Status card
        GUILayout.BeginVertical(FillStyle(CardColor));
        GUILayout.BeginHorizontal();
        var sc = StateColor(robotState);
        GUILayout.Label("<color=#" + ColorUtility.ToHtmlStringRGB(sc) + ">\u25CF</color>  " + robotState.ToUpperInvariant(), TextStyle(16, Color.white, FontStyle.Bold));
        GUILayout.FlexibleSpace();
        string badge = string.IsNullOrEmpty(outcome) ? "NO RESULT" : outcome == "pass" ? "PASS" : outcome.Replace("_", " ").ToUpperInvariant();
        Color badgeColor = string.IsNullOrEmpty(outcome) ? new Color(0.35f, 0.38f, 0.45f) : outcome == "pass" ? new Color(0.20f, 0.62f, 0.35f) : new Color(0.80f, 0.28f, 0.28f);
        GUILayout.Label(badge, ButtonStyle(badgeColor, Color.white, 11), GUILayout.Height(22), GUILayout.Width(badge.Length * 8.5f + 18));
        GUILayout.EndHorizontal();
        GUILayout.Label("Holding: " + (held != null ? held.name : "nothing") + "     Retries: " + retries + "/3", TextStyle(12, new Color(0.70f, 0.74f, 0.82f)));
        // Delivery progress: one segment per object
        GUILayout.BeginHorizontal();
        for (int i = 0; i < 6; i++)
            GUILayout.Box(GUIContent.none, FillStyle(i < deliveredTargets.Count ? new Color(0.30f, 0.80f, 0.45f) : new Color(0.24f, 0.27f, 0.34f)), GUILayout.Height(8));
        GUILayout.EndHorizontal();
        GUILayout.Label("Delivered " + deliveredTargets.Count + " of 6", TextStyle(11, new Color(0.55f, 0.60f, 0.68f)));
        GUILayout.EndVertical();
        GUILayout.Space(8);

        // Keep the command field visible even when the Game view is short.
        GUILayout.Label("AI COMMAND (CLAUDE VISION)", TextStyle(11, new Color(0.55f, 0.60f, 0.68f), FontStyle.Bold));
        GUILayout.BeginHorizontal();
        guiCommand = GUILayout.TextField(guiCommand, GUILayout.Height(28));
        if (GUILayout.Button("Send", ButtonStyle(AccentColor, Color.white), GUILayout.Width(64), GUILayout.Height(28)))
        {
            if (!string.IsNullOrWhiteSpace(guiCommand))
            {
                pendingAgentCommand = guiCommand.Trim();
                EnsureAgent();
            }
        }
        if (GUILayout.Button(listening ? "Stop" : "Speak", ButtonStyle(listening ? new Color(0.80f, 0.28f, 0.28f) : CardColor, Color.white), GUILayout.Width(64), GUILayout.Height(28)))
        {
            if (listening) FinishRecording();
            else StartListening();
            EnsureAgent();
        }
        GUILayout.EndHorizontal();
        if (!string.IsNullOrEmpty(voiceNote)) GUILayout.Label("Voice: " + voiceNote, TextStyle(11, new Color(1f, 0.82f, 0.45f)));
        if (agentProcess != null && agentProcess.HasExited && agentProcess.ExitCode != 0)
            agentNote = "The agent stopped. Check ANTHROPIC_API_KEY in " + agentFolder + "\\.env, or run: python -m agent.run_agent --listen --port " + listenerPort;
        if (!string.IsNullOrEmpty(agentNote)) GUILayout.Label(agentNote, TextStyle(11, new Color(1f, 0.55f, 0.45f)));
        GUILayout.Label("The model sees only the robot camera.", TextStyle(10, new Color(0.70f, 0.74f, 0.82f)));
        GUILayout.Space(8);

        // Camera switcher
        GUILayout.Label("CAMERA  (press C to cycle)", TextStyle(11, new Color(0.55f, 0.60f, 0.68f), FontStyle.Bold));
        GUILayout.BeginHorizontal();
        for (int v = 0; v < viewNames.Length; v++)
        {
            bool active = v == viewIndex;
            if (GUILayout.Button(viewNames[v], ButtonStyle(active ? AccentColor : CardColor, active ? Color.white : new Color(0.75f, 0.79f, 0.86f), 11), GUILayout.Height(26))) SetView(v);
        }
        GUILayout.EndHorizontal();
        GUILayout.Space(8);

        // Speed
        GUILayout.Label("SPEED", TextStyle(11, new Color(0.55f, 0.60f, 0.68f), FontStyle.Bold));
        GUILayout.BeginHorizontal();
        foreach (var sp in speeds)
        {
            bool active = Mathf.Approximately(Time.timeScale, sp);
            if (GUILayout.Button(sp + "x", ButtonStyle(active ? AccentColor : CardColor, active ? Color.white : new Color(0.75f, 0.79f, 0.86f), 12), GUILayout.Height(26))) SetSpeed(sp);
        }
        GUILayout.EndHorizontal();
        GUILayout.Space(8);

        // Modes: dark (demo only) and the detour wall (stretch goal), side by side
        GUILayout.Label("MODES", TextStyle(11, new Color(0.55f, 0.60f, 0.68f), FontStyle.Bold));
        GUILayout.BeginHorizontal();
        if (GUILayout.Button(darkMode ? "Dark mode: ON" : "Dark mode: OFF", ButtonStyle(darkMode ? new Color(0.42f, 0.26f, 0.66f) : CardColor, Color.white, 12), GUILayout.Height(28))) SetDarkMode(!darkMode);
        bool wallOn = obstacle != null && obstacle.gameObject.activeSelf;
        if (GUILayout.Button(wallOn ? "Detour wall: ON" : "Detour wall: OFF", ButtonStyle(wallOn ? new Color(0.72f, 0.46f, 0.10f) : CardColor, Color.white, 12), GUILayout.Height(28))) SetObstacle(!wallOn);
        GUILayout.EndHorizontal();
        GUILayout.Space(8);

        // Prewritten commands
        GUILayout.Label("DEBUG PRESETS (SCRIPTED)", TextStyle(11, new Color(0.55f, 0.60f, 0.68f), FontStyle.Bold));
        GUI.enabled = !running;
        for (int i = 0; i < shapeCommands.Length; i += 2)
        {
            GUILayout.BeginHorizontal();
            for (int j = i; j < i + 2 && j < shapeCommands.Length; j++)
            {
                string name = shapeCommands[j].Substring("Bring the ".Length);
                bool done = deliveredTargets.Contains(name);
                Color fill = done ? new Color(0.22f, 0.25f, 0.31f) : ObjectColor(name);
                Color text = done ? new Color(0.55f, 0.60f, 0.68f) : (name.StartsWith("yellow") ? new Color(0.12f, 0.12f, 0.12f) : Color.white);
                if (GUILayout.Button((done ? "\u2713 " : "") + name, ButtonStyle(fill, text), GUILayout.Height(34)) && !done) StartCoroutine(RunCommand(shapeCommands[j]));
            }
            GUILayout.EndHorizontal();
        }
        GUILayout.Space(4);
        GUILayout.BeginHorizontal();
        if (GUILayout.Button("Round red thing", ButtonStyle(CardColor, Color.white, 12), GUILayout.Height(28))) StartCoroutine(RunCommand("Bring the round red thing"));
        if (GUILayout.Button("Tallest object", ButtonStyle(CardColor, Color.white, 12), GUILayout.Height(28))) StartCoroutine(RunCommand("Bring the tallest object"));
        GUILayout.EndHorizontal();
        GUILayout.Space(4);
        if (GUILayout.Button("RUN ALL OBJECTS (DEBUG)", ButtonStyle(new Color(0.20f, 0.62f, 0.35f), Color.white, 14), GUILayout.Height(38))) StartCoroutine(RunAll());
        GUILayout.Space(8);

        GUI.enabled = true;
        GUILayout.Space(8);
        GUILayout.BeginHorizontal();
        bool resetClicked = GUILayout.Button("RESET SCENE", ButtonStyle(new Color(0.55f, 0.20f, 0.22f), Color.white, 12), GUILayout.Height(28));
        bool shuffleClicked = GUILayout.Button("SHUFFLE LAYOUT", ButtonStyle(CardColor, Color.white, 12), GUILayout.Height(28));
        GUILayout.EndHorizontal();
        if (resetClicked || shuffleClicked)
        {
            StopAllCoroutines();
            running = false;
            busy = false;
            driving = false;
            InternalReset();
            ShuffleLayout(shuffleClicked ? UnityEngine.Random.Range(1, 100000) : 0);
        }
        GUILayout.Space(6);
        if (!string.IsNullOrEmpty(guiStatus)) GUILayout.Label(guiStatus, TextStyle(12, new Color(0.80f, 0.84f, 0.92f), FontStyle.Italic));
        GUILayout.EndVertical();
        GUILayout.EndArea();
    }

    private bool panelHidden;

    // Screen width the sidebar takes, in pixels (0 when hidden). Never more than half the view.
    private float PanelPixels => panelHidden ? 0f : Mathf.Min(Screen.width * 0.5f, 388f * Mathf.Max(1f, Screen.height / 900f));

    // Runs one command, then leaves the scene as-is so the next command continues from here.
    private IEnumerator RunCommand(string command, bool pickupOnly = false, string scoreAs = null)
    {
        if (running) yield break;
        running = true;
        yield return StartCoroutine(ExecuteCommandSequence(command, pickupOnly, scoreAs));
        running = false;
    }

    private IEnumerator RunAll()
    {
        if (running) yield break;
        running = true;
        foreach (var item in objects)
        {
            if (deliveredTargets.Contains(item.name)) continue;
            yield return StartCoroutine(ExecuteCommandSequence("Bring the " + item.name));
            if (outcome != "pass") break;
        }
        running = false;
    }

    private void Handle(HttpListenerContext context)
    {
        string path = context.Request.Url.AbsolutePath;
        string method = context.Request.HttpMethod;
        // Always drain the body first: replying with unread request bytes makes the listener reset the connection.
        requestBody = context.Request.HasEntityBody ? ReadBody(context) : "";
        try
        {
            if (robotState == "Done" && method == "POST" && path != "/reset" && path != "/start" && path != "/run_builtin" && path != "/view" && path != "/speed" && path != "/obstacle" && path != "/dark" && path != "/agent_status" && path != "/agent_heard") { Error(context, 409, "Run finished; start another command or reset"); return; }
            if (method == "GET" && path == "/state")
            {
                var p = robotRoot.position;
                Reply(context, new StateOutput { name = robotState, state = robotState, x = p.x, y = p.z, heading = heading, holding = held != null, retry_count = retries, retries = retries, outcome = outcome });
            }
            else if (method == "POST" && path == "/agent_status")
            {
                // Progress line from the agent process, shown under the AI COMMAND box.
                var input = JsonUtility.FromJson<StartInput>(requestBody);
                if (input != null && !string.IsNullOrEmpty(input.command)) guiStatus = "Agent: " + input.command;
                Reply(context, new ActionOutput { success = true, ok = true });
            }
            else if (method == "GET" && path == "/agent_audio")
            {
                // The latest voice recording (WAV), handed over once.
                var audio = pendingAudio;
                pendingAudio = null;
                if (audio == null) { Error(context, 404, "No recording"); return; }
                ReplyRaw(context, audio, "audio/wav");
            }
            else if (method == "POST" && path == "/agent_heard")
            {
                // What the agent process transcribed: shown in the command box.
                var input = JsonUtility.FromJson<StartInput>(requestBody);
                if (input != null && !string.IsNullOrWhiteSpace(input.command))
                {
                    guiCommand = input.command.Trim();
                    voiceNote = "Heard: \"" + guiCommand + "\"";
                    Debug.Log("Voice command: " + voiceNote);
                }
                else voiceNote = "Could not make out any words. Press Speak and try again.";
                Reply(context, new ActionOutput { success = true, ok = true });
            }
            else if (method == "GET" && path == "/agent_command")
            {
                lastAgentPoll = Time.realtimeSinceStartup;
                string command = pendingAgentCommand;
                pendingAgentCommand = null;
                Reply(context, new CommandOutput { command = command, audio = pendingAudio != null });
            }
            else if (method == "GET" && path == "/look") ReplyImage(context);
            else if (method == "GET" && path == "/depth")
            {
                // Level cast so open floor reads null; small radius so low objects are still hit.
                Vector3 level = Vector3.ProjectOnPlane(robotCamera.transform.forward, Vector3.up).normalized;
                string value = Physics.SphereCast(robotCamera.transform.position, 0.12f, level, out var hit, 10f, ~0, QueryTriggerInteraction.Ignore) ? hit.distance.ToString("0.###", System.Globalization.CultureInfo.InvariantCulture) : "null";
                ReplyRaw(context, Encoding.UTF8.GetBytes(value), "application/json");
            }
            else if (method == "POST" && path == "/start") StartRun(context);
            else if (method == "POST" && path == "/move")
            {
                var input = JsonUtility.FromJson<MoveInput>(requestBody);
                if (input == null || float.IsNaN(input.distance) || float.IsInfinity(input.distance) || Mathf.Abs(input.distance) > 3f) { Error(context, 400, "Invalid distance"); return; }
                StartCoroutine(Move(context, input.distance));
            }
            else if (method == "POST" && path == "/turn")
            {
                var input = JsonUtility.FromJson<TurnInput>(requestBody);
                if (input == null || float.IsNaN(input.degrees) || float.IsInfinity(input.degrees) || Mathf.Abs(input.degrees) > 180f) { Error(context, 400, "Invalid degrees"); return; }
                StartCoroutine(Turn(context, input.degrees));
            }
            else if (method == "POST" && path == "/pickup") StartCoroutine(PickUpAndLift(context));
            else if (method == "POST" && path == "/drop") Drop(context);
            else if (method == "POST" && path == "/fail")
            {
                robotState = "Done";
                outcome = "fail_not_reached";
                recordHold = Mathf.Max(recordHold, 0.6f);
                Reply(context, new ActionOutput { success = true, ok = true });
            }
            else if (method == "POST" && path == "/run_builtin")
            {
                // Built-in (non-Claude) command runner; uses engine-side positions, so it is a demo/test path only.
                var input = JsonUtility.FromJson<StartInput>(requestBody);
                if (input == null || string.IsNullOrWhiteSpace(input.command)) { Error(context, 400, "Command is required"); return; }
                string wanted = ResolveTarget(input.command);
                if (wanted == null) { Error(context, 400, "Built-in runner needs a colour/shape command"); return; }
                if (deliveredTargets.Contains(wanted)) { Error(context, 409, "Already delivered; reset first"); return; }
                if (running) { Error(context, 409, "A command is already running"); return; }
                robotState = "Locate"; // so pollers do not see the previous run's Done
                StartRecording(input.record_dir);
                // score_as (tests only): fetch one object but judge the drop against another command.
                StartCoroutine(RunCommand(input.command, input.pickup_only, string.IsNullOrEmpty(input.score_as) ? null : ResolveTarget(input.score_as)));
                Reply(context, new ActionOutput { success = true, ok = true });
            }
            else if (method == "GET" && path == "/record")
                Reply(context, new RecordOutput { recording = recording, frames = recordFrames, fps = ClipFps, width = ClipWidth, height = ClipHeight });
            // Operator/test endpoints (not given to Claude): display camera and a screenshot of the Game view.
            else if (method == "GET" && path == "/view") Reply(context, new ViewOutput { index = viewIndex, view = viewNames[viewIndex] });
            else if (method == "POST" && path == "/view")
            {
                var input = JsonUtility.FromJson<ViewInput>(requestBody);
                SetView(input != null ? input.index : viewIndex + 1);
                Reply(context, new ViewOutput { index = viewIndex, view = viewNames[viewIndex] });
            }
            else if (method == "GET" && path == "/screen") StartCoroutine(ReplyScreen(context));
            else if (method == "GET" && path == "/speed") Reply(context, new SpeedOutput { scale = Time.timeScale });
            else if (method == "POST" && path == "/speed")
            {
                var input = JsonUtility.FromJson<SpeedInput>(requestBody);
                if (input != null && input.scale >= 0.25f && input.scale <= 8f) SetSpeed(input.scale);
                Reply(context, new SpeedOutput { scale = Time.timeScale });
            }
            else if (method == "GET" && path == "/dark") Reply(context, new ObstacleOutput { on = darkMode });
            else if (method == "POST" && path == "/dark")
            {
                var input = JsonUtility.FromJson<ObstacleInput>(requestBody);
                SetDarkMode(input != null && input.on);
                Reply(context, new ObstacleOutput { on = darkMode });
            }
            else if (method == "GET" && path == "/obstacle") Reply(context, new ObstacleOutput { on = obstacle != null && obstacle.gameObject.activeSelf });
            else if (method == "POST" && path == "/obstacle")
            {
                var input = JsonUtility.FromJson<ObstacleInput>(requestBody);
                SetObstacle(input != null && input.on);
                Reply(context, new ObstacleOutput { on = obstacle != null && obstacle.gameObject.activeSelf });
            }
            else if (method == "POST" && path == "/go_to")
            {
                // Allowed by the brief: the engine does the path-finding to a point the agent chose.
                var input = JsonUtility.FromJson<GoToInput>(requestBody);
                bool valid = input != null && !float.IsNaN(input.x) && !float.IsNaN(input.y) && !float.IsInfinity(input.x) && !float.IsInfinity(input.y)
                    && input.x >= planBounds.min.x - 0.5f && input.x <= planBounds.max.x + 0.5f
                    && input.y >= planBounds.min.z - 0.5f && input.y <= planBounds.max.z + 0.5f;
                if (!valid) { Error(context, 400, "Invalid target"); return; }
                StartCoroutine(GoTo(context, new Vector2(input.x, input.y), Mathf.Clamp(input.stop_short, 0f, 2f)));
            }
            else if (method == "POST" && path == "/reset") ResetScene(context);
            else Error(context, 404, "Unknown endpoint");
        }
        catch (Exception error) { Debug.LogException(error); Error(context, 500, "Engine error"); }
    }

    private IEnumerator MoveRoutine(float distance)
    {
        busy = true;
        robotState = held == null ? "Go to object" : "Carry";
        float remaining = Mathf.Abs(distance);
        float sign = Mathf.Sign(distance);
        bool collided = false;
        lastMoved = 0f;
        lastCollided = false;
        while (remaining > 0.0001f)
        {
            float step = Mathf.Min(remaining, Time.deltaTime); // 1 m/s
            Vector3 direction = Forward() * sign;
            bool bodyHit = TryGetMovementObstacle(robotRoot.position, direction, step + 0.01f, out var obstacle);
            Vector3 gripperPoint = default;
            if (bodyHit || (sign > 0 && GripperBlocked(direction, step + 0.01f, out gripperPoint)))
            {
                collided = true;
                Vector3 hitPoint = bodyHit ? obstacle.point : gripperPoint;
                lastHitPoint = new Vector2(hitPoint.x, hitPoint.z);
                retries++;
                robotState = retries >= 3 ? "Done" : "Blocked";
                if (retries >= 3) { outcome = "fail_collision"; recordHold = Mathf.Max(recordHold, 1.0f); }
                break;
            }
            robotRoot.position += direction * step;
            remaining -= step;
            lastMoved += step;
            yield return null;
        }
        if (collided)
        {
            // Back off 0.1 m over a few frames so the bump is visible, unless something is behind.
            Vector3 back = -Forward() * sign;
            float backed = 0f;
            while (backed < 0.1f && !TryGetMovementObstacle(robotRoot.position, back, 0.02f, out _))
            {
                float b = Mathf.Min(0.1f - backed, Time.deltaTime * 0.8f);
                robotRoot.position += back * b;
                backed += b;
                yield return null;
            }
            lastMoved -= backed;
            recordHold = Mathf.Max(recordHold, 0.3f);
        }
        lastCollided = collided;
        lastMoved *= sign;
        if (!collided) robotState = held == null ? "Locate" : "Carry";
        busy = false;
    }

    private IEnumerator TurnRoutine(float degrees)
    {
        busy = true;
        lastCollided = false;
        float remaining = Mathf.Abs(degrees);
        float sign = Mathf.Sign(degrees);
        while (remaining > 0.001f)
        {
            float step = Mathf.Min(remaining, Time.deltaTime * 90f);
            float next = Mathf.Repeat(heading + sign * step, 360f);
            // Only a turn that brings the gripper INTO contact counts, so a robot already touching
            // something can always turn away from it.
            if (GripperOverlaps(next, out var hitPoint) && !GripperOverlaps(heading, out _))
            {
                lastCollided = true;
                lastHitPoint = new Vector2(hitPoint.x, hitPoint.z);
                retries++;
                robotState = retries >= 3 ? "Done" : "Blocked";
                if (retries >= 3) outcome = "fail_collision";
                recordHold = Mathf.Max(recordHold, retries >= 3 ? 1.0f : 0.3f);
                busy = false;
                yield break;
            }
            heading = next;
            robotRoot.rotation = Quaternion.Euler(0f, 90f + heading, 0f);
            remaining -= step;
            yield return null;
        }
        robotState = held == null ? "Locate" : "Carry";
        busy = false;
    }

    private IEnumerator Move(HttpListenerContext context, float distance)
    {
        yield return StartCoroutine(MoveRoutine(distance));
        Reply(context, new ActionOutput { success = !lastCollided, ok = !lastCollided, collided = lastCollided, moved = lastMoved });
    }

    private IEnumerator Turn(HttpListenerContext context, float degrees)
    {
        yield return StartCoroutine(TurnRoutine(degrees));
        Reply(context, new ActionOutput { success = !lastCollided, ok = !lastCollided, collided = lastCollided, heading = heading });
    }

    private bool InternalPickUp()
    {
        robotState = "Pick up";
        if (held != null) { robotState = "Carry"; return false; }
        Transform nearest = null;
        float best = Reach;
        foreach (var item in objects)
        {
            if (item == null || item.parent == transform || deliveredTargets.Contains(item.name)) continue;
            Vector3 delta = item.position - robotRoot.position;
            delta.y = 0;
            float angle = Vector3.Angle(Forward(), delta);
            if (delta.magnitude < best && angle <= 30f) { nearest = item; best = delta.magnitude; }
        }
        if (nearest == null) { robotState = "Locate"; return false; }
        held = nearest;
        held.SetParent(robotRoot, true);
        held.localRotation = Quaternion.identity;
        var heldRenderer = held.GetComponent<Renderer>();
        Vector3 extents = heldRenderer != null ? heldRenderer.bounds.extents : Vector3.one * 0.24f;
        gripClosed = Mathf.Max(extents.x, extents.z) + 0.03f; // finger centre just outside the object
        holdPose = new Vector3(0f, CarryHeight + extents.y, HoldDistance); // raised clear of the camera's view
        var collider = held.GetComponent<Collider>();
        if (collider != null) collider.enabled = false;
        robotState = "Carry";
        recordHold = Mathf.Max(recordHold, 0.6f);
        return true;
    }

    private bool InternalDrop(out bool inZone)
    {
        robotState = "Drop";
        inZone = false;
        if (held == null) { robotState = "Locate"; return false; }
        // Release 0.75 m ahead (clear of the 0.4 m robot body) of the robot's real position, not at an assigned slot.
        Vector3 dropPoint = robotRoot.position + Forward() * DropDistance;
        string droppedName = held.name;
        held.SetParent(null, true);
        held.rotation = Quaternion.identity; // set down upright and square to the zone
        var itemRenderer = held.GetComponent<Renderer>();
        float halfHeight = itemRenderer != null ? itemRenderer.bounds.extents.y : 0.24f;
        // Released near the assigned slot: centre it there so the layout stays tidy.
        if (Vector2.Distance(new Vector2(dropPoint.x, dropPoint.z), new Vector2(currentDropPosition.x, currentDropPosition.z)) < 0.6f)
            dropPoint = new Vector3(currentDropPosition.x, dropPoint.y, currentDropPosition.z);
        held.position = new Vector3(dropPoint.x, floorHeight + halfHeight, dropPoint.z);
        var collider = held.GetComponent<Collider>();
        if (collider != null) collider.enabled = true;
        held = null;
        // Zone is a 3.6 x 1.5 box (see BuildScene).
        inZone = Mathf.Abs(dropPoint.x - zoneCenter.x) < 1.8f && Mathf.Abs(dropPoint.z - zoneCenter.z) < 0.75f;
        robotState = "Done";
        // ponytail: targetName null = command the engine can't resolve; any object in the zone passes, upgrade to a judge-side scorer.
        bool correct = targetName == null || droppedName == targetName;
        outcome = !inZone ? "fail_not_reached" : correct ? "pass" : "fail_wrong_object";
        if (inZone && correct)
        {
            deliveredTargets.Add(droppedName);
            // Delivered objects stay solid so the robot drives around them.
        }
        recordHold = Mathf.Max(recordHold, 1.0f); // end the clip on the released object
        return true;
    }

    // Neat 3 x 2 grid in the zone. Fill the far row first (so the robot never drives through
    // delivered objects to reach it), taking the free slot nearest the robot within a row.
    private Vector3 GetDeliverySlot(string name)
    {
        var y = floorHeight + 0.27f;
        if (name == null) return new Vector3(zoneCenter.x, y, zoneCenter.z);
        Vector2 robotPos = new Vector2(robotRoot.position.x, robotRoot.position.z);
        Vector2 best = new Vector2(zoneCenter.x, zoneCenter.z);
        float bestScore = float.MaxValue;
        for (int row = 0; row < 2; row++)
        for (int column = 0; column < 3; column++)
        {
            var c = new Vector2(zoneCenter.x + (column - 1) * 1.2f, zoneCenter.z + (row == 0 ? -0.4f : 0.4f));
            bool taken = false;
            foreach (var item in objects)
                if (item != null && item.name != name && deliveredTargets.Contains(item.name)
                    && Vector2.Distance(c, new Vector2(item.position.x, item.position.z)) < 0.6f) taken = true;
            if (taken) continue;
            float score = row * 100f + Vector2.Distance(robotPos, c);
            if (score < bestScore) { best = c; bestScore = score; }
        }
        return new Vector3(best.x, y, best.y);
    }

    private IEnumerator PickUpAndLift(HttpListenerContext context)
    {
        bool ok = InternalPickUp();
        if (ok)
        {
            busy = true; // the lift is robot motion: the scene advances and the clip records it
            float limit = Time.time + 3f;
            while (held != null && Vector3.Distance(held.localPosition, holdPose) > 0.01f && Time.time < limit) yield return null;
            busy = false;
        }
        Reply(context, new ActionOutput { success = ok, ok = ok });
    }

    private void PickUp(HttpListenerContext context)
    {
        bool ok = InternalPickUp();
        Reply(context, new ActionOutput { success = ok, ok = ok });
    }

    private void Drop(HttpListenerContext context)
    {
        bool inZone;
        bool ok = InternalDrop(out inZone);
        Reply(context, new ActionOutput { success = ok, ok = ok, in_zone = inZone });
    }

    private void StartRun(HttpListenerContext context)
    {
        var input = JsonUtility.FromJson<StartInput>(requestBody);
        if (input == null || string.IsNullOrWhiteSpace(input.command)) { Error(context, 400, "Command is required"); return; }
        if (held != null || busy) { Error(context, 409, "Run already started"); return; }
        targetName = ResolveTarget(input.command);
        // Unresolvable commands are accepted; Claude interprets them from pixels.
        if (targetName != null && deliveredTargets.Contains(targetName)) { Error(context, 409, "Object already delivered; choose another object or reset"); return; }
        retries = 0;
        outcome = null;
        currentDropPosition = GetDeliverySlot(targetName);
        robotState = "Locate";
        StartRecording(input.record_dir); // state just left Idle: the clip starts here
        Reply(context, new ActionOutput { success = true });
    }

    private void InternalReset()
    {
        if (held != null)
        {
            held.SetParent(null, true);
            var heldCollider = held.GetComponent<Collider>();
            if (heldCollider != null) heldCollider.enabled = true;
            held = null;
        }
        if (objects != null && spawnPositions != null)
        {
            for (int i = 0; i < objects.Length; i++)
            {
                if (objects[i] == null) continue;
                objects[i].SetParent(null, false);
                objects[i].position = spawnPositions[i];
                objects[i].rotation = spawnRotations[i];
                var col = objects[i].GetComponent<Collider>();
                if (col != null) col.enabled = true;
            }
        }
        deliveredTargets.Clear();
        currentDropPosition = zoneCenter;
        robotRoot.position = robotStart;
        robotRoot.rotation = Quaternion.Euler(0, 90, 0);
        heading = 0f;
        retries = 0;
        outcome = null;
        targetName = null;
        busy = false; driving = false;
        robotState = "Idle";
        guiStatus = "Idle. Ready for command.";
    }

    private int layoutSeed;

    // Seeded layout: permute the six known-clear spawn spots and nudge each by up to 0.25 m.
    // Seed 0 is the default layout. The same seed always gives the same layout.
    private void ShuffleLayout(int seed)
    {
        layoutSeed = seed;
        if (seed == 0 || objects == null || spawnPositions == null) return;
        var rng = new System.Random(seed);
        int n = objects.Length;
        var order = new int[n];
        for (int i = 0; i < n; i++) order[i] = i;
        for (int i = n - 1; i > 0; i--) { int j = rng.Next(i + 1); int t = order[i]; order[i] = order[j]; order[j] = t; }
        for (int i = 0; i < n; i++)
        {
            var spot = spawnPositions[order[i]];
            float dx = (float)(rng.NextDouble() - 0.5) * 0.5f, dz = (float)(rng.NextDouble() - 0.5) * 0.5f;
            objects[i].position = new Vector3(spot.x + dx, spawnPositions[i].y, spot.z + dz); // keep each object's own height
        }
        guiStatus = "Shuffled layout, seed " + seed + ".";
    }

    private void ResetScene(HttpListenerContext context)
    {
        var input = string.IsNullOrWhiteSpace(requestBody) ? null : JsonUtility.FromJson<ResetInput>(requestBody);
        // A reset over HTTP is authoritative, like the panel button: stop any sequence still running.
        StopAllCoroutines();
        running = false;
        driving = false;
        InternalReset();
        ShuffleLayout(input != null ? input.seed : 0);
        Reply(context, new ActionOutput { success = true, ok = true });
    }

    private Vector2 GetTargetPos(string name)
    {
        if (objects == null) return Vector2.zero;
        foreach (var item in objects)
            if (item != null && item.name == name && !deliveredTargets.Contains(item.name))
                return new Vector2(item.position.x, item.position.z);
        return Vector2.zero;
    }

    private IEnumerator ExecuteLeg(Vector2 start, Vector2 end, float stopShort)
    {
        float dx = end.x - start.x;
        float dy = end.y - start.y;
        float length = Mathf.Sqrt(dx * dx + dy * dy);
        float desired = Mathf.Repeat(Mathf.Atan2(-dy, dx) * Mathf.Rad2Deg, 360f);
        float distance = Mathf.Max(0f, length - stopShort);
        float turn = Mathf.DeltaAngle(heading, desired);
        if (Mathf.Abs(turn) > 0.01f)
        {
            yield return StartCoroutine(TurnRoutine(turn));
            if (lastCollided) yield break; // DriveTo re-plans around the spot
        }
        while (distance > 0.001f)
        {
            float step = Mathf.Min(1.0f, distance);
            yield return StartCoroutine(MoveRoutine(step));
            if (lastCollided) yield break;
            distance -= step;
        }
    }

    // ---- Shortest-path planning: A* on a 0.2 m grid, then line-of-sight smoothing. ----
    private const float Cell = 0.2f;
    private int gridW, gridH, gridStart;
    private sbyte[] gridCache;
    private Transform gridIgnore;
    private Vector2 lastHitPoint;
    private readonly List<Vector2> blockedSpots = new List<Vector2>();
    private float gridMargin = 0.1f;

    private Vector2 CellCenter(int x, int z) => new Vector2(planBounds.min.x + (x + 0.5f) * Cell, planBounds.min.z + (z + 0.5f) * Cell);
    private int CellOf(Vector2 p) => Mathf.Clamp(Mathf.FloorToInt((p.y - planBounds.min.z) / Cell), 0, gridH - 1) * gridW + Mathf.Clamp(Mathf.FloorToInt((p.x - planBounds.min.x) / Cell), 0, gridW - 1);

    private bool CellBlocked(int x, int z)
    {
        if (x < 0 || z < 0 || x >= gridW || z >= gridH) return true;
        int i = z * gridW + x;
        if (i == gridStart) return false;
        var cellPoint = CellCenter(x, z);
        foreach (var spot in blockedSpots) if (Vector2.Distance(spot, cellPoint) < 0.5f) return true; // spots where a move was blocked
        if (gridCache[i] == 0)
        {
            sbyte result = 1;
            var c = CellCenter(x, z);
            float r = Radius * 0.8f + gridMargin;
            var low = new Vector3(c.x, floorHeight + 0.22f, c.y);
            var high = new Vector3(c.x, floorHeight + Mathf.Max(0.22f, RobotTop - r), c.y);
            foreach (var col in Physics.OverlapCapsule(low, high, r, ~0, QueryTriggerInteraction.Ignore))
            {
                var t = col.transform;
                if (t == robotRoot || t.IsChildOf(robotRoot)) continue;
                if (gridIgnore != null && (t == gridIgnore || t.IsChildOf(gridIgnore))) continue;
                if (col.bounds.max.y <= floorHeight + 0.08f) continue;
                result = -1; break;
            }
            gridCache[i] = result;
        }
        return gridCache[i] < 0;
    }

    private bool LineFree(Vector2 a, Vector2 b)
    {
        float len = Vector2.Distance(a, b);
        for (float d = 0; d <= len; d += Cell * 0.5f)
        {
            int i = CellOf(Vector2.Lerp(a, b, len < 0.0001f ? 0 : d / len));
            if (CellBlocked(i % gridW, i / gridW)) return false;
        }
        return true;
    }

    // Returns waypoints from start to goal (inclusive), or null if no route exists.
    private List<Vector2> PlanPath(Vector2 start, Vector2 goal, Transform ignore)
    {
        gridW = Mathf.CeilToInt(planBounds.size.x / Cell);
        gridH = Mathf.CeilToInt(planBounds.size.z / Cell);
        gridCache = new sbyte[gridW * gridH];
        gridIgnore = ignore;
        gridStart = CellOf(start);
        int goalCell = CellOf(goal);
        int n = gridW * gridH;
        var cost = new float[n];
        var parent = new int[n];
        var closed = new bool[n];
        for (int i = 0; i < n; i++) { cost[i] = float.MaxValue; parent[i] = -1; }
        var open = new List<int> { gridStart };
        cost[gridStart] = 0;
        Vector2 goalPoint = CellCenter(goalCell % gridW, goalCell / gridW);
        bool found = false;
        while (open.Count > 0)
        {
            int bestIndex = 0; float bestF = float.MaxValue;
            for (int i = 0; i < open.Count; i++)
            {
                var c = CellCenter(open[i] % gridW, open[i] / gridW);
                float f = cost[open[i]] + Vector2.Distance(c, goalPoint);
                if (f < bestF) { bestF = f; bestIndex = i; }
            }
            int cur = open[bestIndex];
            open.RemoveAt(bestIndex);
            if (cur == goalCell) { found = true; break; }
            closed[cur] = true;
            int cx = cur % gridW, cz = cur / gridW;
            for (int dx = -1; dx <= 1; dx++)
            for (int dz = -1; dz <= 1; dz++)
            {
                if (dx == 0 && dz == 0) continue;
                int nx = cx + dx, nz = cz + dz;
                if (nx < 0 || nz < 0 || nx >= gridW || nz >= gridH) continue;
                int ni = nz * gridW + nx;
                if (closed[ni]) continue;
                if (ni != goalCell && CellBlocked(nx, nz)) continue;
                if (dx != 0 && dz != 0 && (CellBlocked(cx + dx, cz) || CellBlocked(cx, cz + dz))) continue; // no corner cutting
                float g = cost[cur] + (dx != 0 && dz != 0 ? 1.4142f : 1f) * Cell;
                if (g < cost[ni]) { cost[ni] = g; parent[ni] = cur; if (!open.Contains(ni)) open.Add(ni); }
            }
        }
        if (!found) return null;
        var cells = new List<Vector2>();
        for (int i = goalCell; i != -1; i = parent[i]) cells.Add(CellCenter(i % gridW, i / gridW));
        cells.Reverse();
        cells[0] = start;
        cells[cells.Count - 1] = goal;
        // Smooth: from each waypoint jump to the farthest one still in line of sight.
        var path = new List<Vector2> { cells[0] };
        int at = 0;
        while (at < cells.Count - 1)
        {
            int next = at + 1;
            for (int j = cells.Count - 1; j > at + 1; j--) if (LineFree(cells[at], cells[j])) { next = j; break; }
            path.Add(cells[next]);
            at = next;
        }
        return path;
    }

    // Drive to goal along the shortest planned path; if a move is blocked, mark that spot and plan another route.
    private bool driving;

    // POST /go_to: shortest collision-free route to (x, y), stopping stop_short metres before it.
    // The reply says only whether the robot got there; nothing about what is at the point.
    private IEnumerator GoTo(HttpListenerContext context, Vector2 goal, float stopShort)
    {
        driving = true;
        lastCollided = false;
        // A point on top of a loose object cannot be driven onto: path up to it and stop at arm's length.
        Transform ignore = null;
        float best = 0.6f;
        foreach (var item in objects)
        {
            if (item == null || item == held || deliveredTargets.Contains(item.name)) continue;
            float d = Vector2.Distance(goal, new Vector2(item.position.x, item.position.z));
            if (d < best) { best = d; ignore = item; }
        }
        if (ignore != null) stopShort = Mathf.Max(stopShort, 0.76f);
        yield return StartCoroutine(DriveTo(goal, stopShort, ignore));
        driving = false;
        var at = new Vector2(robotRoot.position.x, robotRoot.position.z);
        bool reached = !lastCollided && Vector2.Distance(at, goal) <= stopShort + 0.35f;
        Reply(context, new GoToOutput { ok = reached, collided = lastCollided, reached = reached, x = at.x, y = at.y, heading = heading });
    }

    private IEnumerator DriveTo(Vector2 goal, float stopShort, Transform ignore)
    {
        blockedSpots.Clear();
        for (int attempt = 0; attempt < 4; attempt++)
        {
            Vector2 start = new Vector2(robotRoot.position.x, robotRoot.position.z);
            gridMargin = 0.1f;
            var path = PlanPath(start, goal, ignore);
            if (path == null) { gridMargin = 0.03f; path = PlanPath(start, goal, ignore); } // tighter clearance before giving up
            gridMargin = 0.1f;
            if (path == null) path = new List<Vector2> { start, goal };
            float trim = stopShort;
            while (trim > 0.001f && path.Count > 1)
            {
                Vector2 a = path[path.Count - 2], b = path[path.Count - 1];
                float len = (b - a).magnitude;
                if (len > trim) { path[path.Count - 1] = b - (b - a) / len * trim; trim = 0; }
                else { trim -= len; path.RemoveAt(path.Count - 1); }
            }
            bool blocked = false;
            for (int i = 1; i < path.Count && !blocked; i++)
            {
                yield return StartCoroutine(ExecuteLeg(path[i - 1], path[i], 0f));
                blocked = lastCollided;
            }
            if (!blocked || robotState == "Done") yield break; // arrived, or out of retries
            blockedSpots.Add(lastHitPoint);
            guiStatus = "Blocked, finding another route...";
            // Give the gripper room to turn before trying the new route.
            if (!TryGetMovementObstacle(robotRoot.position, -Forward(), 0.35f, out _))
                yield return StartCoroutine(MoveRoutine(-0.3f));
        }
    }

    private IEnumerator ExecuteCommandSequence(string cmd, bool pickupOnly = false, string scoreAs = null)
    {
        yield return null;
        targetName = ResolveTarget(cmd);
        if (targetName == null)
        {
            guiStatus = $"Target not found for: '{cmd}'";
            yield break;
        }
        if (deliveredTargets.Contains(targetName))
        {
            guiStatus = $"{targetName} was already delivered. Choose another object or press Reset.";
            yield break;
        }
        retries = 0;
        outcome = null;
        currentDropPosition = GetDeliverySlot(targetName);
        guiStatus = $"Target: {targetName}. Approaching...";
        // Like a real robot: back away from anything at the gripper (e.g. what it just placed) before turning.
        if (GripperNear(0.5f) && !TryGetMovementObstacle(robotRoot.position, -Forward(), 0.55f, out _))
            yield return StartCoroutine(MoveRoutine(-0.5f));
        robotState = "Locate";
        Vector2 targetPos = GetTargetPos(targetName);
        Transform targetTransform = null;
        foreach (var item in objects) if (item.name == targetName) targetTransform = item;
        Vector2 startPos = new Vector2(robotRoot.position.x, robotRoot.position.z);
        // Start each task where the previous task left the robot.
        yield return StartCoroutine(DriveTo(targetPos, 0.76f, targetTransform));
        if (lastCollided) { guiStatus = "Collided on approach!"; yield break; }

        // Pick up
        yield return new WaitForSeconds(0.1f);
        bool picked = InternalPickUp();
        if (!picked)
        {
            guiStatus = "Pick up failed (not in reach)!";
            yield break;
        }
        if (pickupOnly) { guiStatus = $"Picked up {targetName}; stopping here (pickup only)."; yield break; }
        guiStatus = $"Picked up {targetName}! Carrying it to the drop zone...";
        yield return new WaitForSeconds(0.15f);

        // Approach the assigned slot, leaving the robot 0.4m behind the object.
        Vector2 slotPos = new Vector2(currentDropPosition.x, currentDropPosition.z);
        guiStatus = "Delivering to drop zone...";
        yield return StartCoroutine(DriveTo(slotPos, DropDistance, null));
        if (lastCollided) { guiStatus = "Collided delivering to zone!"; yield break; }

        // Drop
        yield return new WaitForSeconds(0.1f);
        bool inZone;
        if (scoreAs != null) targetName = scoreAs;
        InternalDrop(out inZone);
        guiStatus = $"Completed! Outcome: {outcome}";
        busy = false;
    }

    private string ResolveTarget(string command)
    {
        string text = command.ToLowerInvariant();
        if (text.Contains("tallest")) return "yellow cylinder";
        string color = text.Contains("red") ? "red" : text.Contains("blue") ? "blue" : text.Contains("yellow") ? "yellow" : null;
        string shape = text.Contains("cube") || text.Contains("square") || text.Contains("box") ? "cube" :
            text.Contains("sphere") || text.Contains("round") || text.Contains("ball") ? "sphere" :
            text.Contains("cylinder") ? "cylinder" : text.Contains("cone") ? "cone" : null;
        if (color != null && shape != null) return color + " " + shape;
        if (color != null)
            foreach (var item in objects) if (!deliveredTargets.Contains(item.name) && item.name.StartsWith(color + " ", StringComparison.Ordinal)) return item.name;
        if (shape != null)
            foreach (var item in objects) if (!deliveredTargets.Contains(item.name) && item.name.EndsWith(" " + shape, StringComparison.Ordinal)) return item.name;
        return null;
    }

    private Vector3 Forward() => new Vector3(Mathf.Cos(heading * Mathf.Deg2Rad), 0f, -Mathf.Sin(heading * Mathf.Deg2Rad));

    private bool TryGetMovementObstacle(Vector3 origin, Vector3 direction, float distance, out RaycastHit nearest)
    {
        nearest = default;
        float nearestDistance = float.MaxValue;
        bool found = false;
        // A capsule over the robot's whole height, so shelves, beams and overhangs count, not just things at wheel level.
        Vector3 low = origin + Vector3.up * 0.22f, high = origin + Vector3.up * (RobotTop - Radius * 0.8f);
        foreach (var hit in Physics.CapsuleCastAll(low, high, Radius * 0.8f, direction, distance, ~0, QueryTriggerInteraction.Ignore))
        {
            if (hit.collider == null || hit.transform == robotRoot || hit.transform.IsChildOf(robotRoot)) continue;
            // Ignore shallow warehouse floor meshes beneath the robot; keep all
            // shelves, fixtures, walls, and task shapes as real movement obstacles.
            if (hit.collider.bounds.max.y <= floorHeight + 0.08f) continue;
            if (hit.distance < nearestDistance)
            {
                nearestDistance = hit.distance;
                nearest = hit;
                found = true;
            }
        }
        return found;
    }

    private void ReplyImage(HttpListenerContext context)
    {
        robotCamera.Render();
        RenderTexture.active = cameraTexture;
        cameraPixels.ReadPixels(new Rect(0, 0, 640, 480), 0, 0);
        cameraPixels.Apply();
        RenderTexture.active = null;
        if (darkMode) ApplyInfrared(cameraPixels);
        ReplyRaw(context, cameraPixels.EncodeToJPG(85), "image/jpeg");
    }

    private IEnumerator ReplyScreen(HttpListenerContext context)
    {
        yield return new WaitForEndOfFrame();
        var shot = ScreenCapture.CaptureScreenshotAsTexture();
        byte[] jpg = shot.EncodeToJPG(85);
        Destroy(shot);
        ReplyRaw(context, jpg, "image/jpeg");
    }

    private string requestBody = "";

    private static string ReadBody(HttpListenerContext context)
    {
        using (var reader = new StreamReader(context.Request.InputStream)) return reader.ReadToEnd();
    }
    private static void Reply(HttpListenerContext context, object value) => ReplyRaw(context, Encoding.UTF8.GetBytes(JsonUtility.ToJson(value)), "application/json");
    private static void Error(HttpListenerContext context, int status, string message)
    {
        context.Response.StatusCode = status;
        ReplyRaw(context, Encoding.UTF8.GetBytes("{\"error\":\"" + message + "\"}"), "application/json");
    }
    private static void ReplyRaw(HttpListenerContext context, byte[] bytes, string type)
    {
        context.Response.ContentType = type;
        context.Response.ContentLength64 = bytes.Length;
        context.Response.OutputStream.Write(bytes, 0, bytes.Length);
        context.Response.Close();
    }

    private void OnDestroy()
    {
        listening = false;
        try { if (agentProcess != null && !agentProcess.HasExited) agentProcess.Kill(); } catch (Exception) { } // the agent we started ends with Play
#if UNITY_EDITOR_WIN || UNITY_STANDALONE_WIN
        if (voiceDevice != null && Microphone.IsRecording(voiceDevice)) Microphone.End(voiceDevice);
#endif
        if (listener != null) { listener.Stop(); listener.Close(); listener = null; }
        if (listenerThread != null && listenerThread.IsAlive) listenerThread.Join(1000);
        if (cameraTexture != null) cameraTexture.Release();
    }

    private static GameObject Box(string name, Vector3 position, Vector3 scale, Color color)
    {
        var box = GameObject.CreatePrimitive(PrimitiveType.Cube);
        box.name = name; box.transform.position = position; box.transform.localScale = scale;
        box.GetComponent<Renderer>().material.color = color;
        return box;
    }

    private void BuildScene()
    {
        // The imported URPScene supplies the complete warehouse and its lighting.
        // The warehouse has several separate objects named Floor. Use the largest
        // floor renderer to frame the whole building and place the task objects.
        Renderer floorRenderer = null;
        foreach (var renderer in FindObjectsByType<Renderer>())
            if (renderer.gameObject.name.Equals("Floor", StringComparison.OrdinalIgnoreCase)
                && (floorRenderer == null || renderer.bounds.size.x * renderer.bounds.size.z > floorRenderer.bounds.size.x * floorRenderer.bounds.size.z))
                floorRenderer = renderer;
        float floorY = floorRenderer != null ? floorRenderer.bounds.max.y : 0.04f;
        planBounds = floorRenderer != null ? floorRenderer.bounds : new Bounds(new Vector3(8.1f, floorY, 5.4f), new Vector3(16.2f, 0.1f, 10.8f));
        floorHeight = floorY;
        Bounds warehouseBounds = floorRenderer != null ? floorRenderer.bounds : new Bounds(new Vector3(8.1f, floorY, 5.4f), new Vector3(16.2f, 0.1f, 10.8f));
        // Use the camera authored inside the warehouse sample. It already looks into
        // the room; the separate TurtleBot POV camera remains available to /look.
        var interiorCameraObject = GameObject.Find("Hero Camera");
        if (interiorCameraObject != null)
        {
            overviewCamera = interiorCameraObject.GetComponent<Camera>();
            if (overviewCamera != null)
            {
                overviewCamera.enabled = true;
                overviewCamera.targetTexture = null;
                overviewCamera.rect = new Rect(0f, 0f, 1f, 1f);
                overviewCamera.depth = 0f;
                overviewCamera.fieldOfView = 58f;
                overviewCamera.nearClipPlane = 0.1f;
                overviewCamera.farClipPlane = 100f;
                if (interiorCameraObject.TryGetComponent<AudioListener>(out var listener)) listener.enabled = true;
            }
        }
        foreach (var camera in FindObjectsByType<Camera>())
            if (camera != overviewCamera) camera.enabled = false;
        var oldRobot = sceneRobotVisualSource != null ? sceneRobotVisualSource.transform : null;
        if (oldRobot != null) oldRobot.gameObject.SetActive(false);
        var robotController = new GameObject("RoboHalo Robot Controller");
        // Place at the warehouse floor first; finalized floor height is applied below.
        robotController.transform.position = new Vector3(robotStart.x, 0f, robotStart.z);
        robotController.transform.rotation = Quaternion.Euler(0, 90, 0);
        robotRoot = robotController.transform;
        AttachTurtleBotVisual(robotRoot);
        BuildGripper(robotRoot);
        var robotCollider = robotController.AddComponent<CapsuleCollider>();
        robotCollider.radius = Radius;
        robotCollider.height = 0.35f;
        robotCollider.center = new Vector3(0, 0.18f, 0);
        // Robot physics uses the same warehouse floor height as the rendered objects.
        layoutHalfX = Mathf.Min(3f, Mathf.Max(2f, warehouseBounds.extents.x * 0.42f));
        layoutRowZ = Mathf.Min(1.4f, Mathf.Max(0.8f, warehouseBounds.extents.z * 0.30f));
        layoutEndZ = Mathf.Min(2.25f, Mathf.Max(1.25f, warehouseBounds.extents.z * 0.58f));
        layoutFarZ = Mathf.Min(layoutEndZ + 1.6f, warehouseBounds.extents.z - 0.6f);
        layoutCenter = warehouseBounds.center;
        robotStart = new Vector3(layoutCenter.x, floorY + 0.01f, layoutCenter.z + layoutEndZ);
        zoneCenter = new Vector3(layoutCenter.x, floorY, layoutCenter.z - layoutEndZ);
        zoneCenter.y = floorY;
        robotRoot.position = robotStart;
        var zone = Box("Drop zone", new Vector3(zoneCenter.x, floorY + 0.025f, zoneCenter.z), new Vector3(3.6f, 0.05f, 1.5f), Color.green);
        zone.GetComponent<Collider>().isTrigger = true;
        currentDropPosition = GetDeliverySlot(null);
        objects = new Transform[6];
        objects[0] = MakeObject(PrimitiveType.Sphere, "red sphere", new Vector3(layoutCenter.x - layoutHalfX, floorY + 0.27f, layoutCenter.z - layoutRowZ), PaintRed);
        objects[1] = MakeObject(PrimitiveType.Cube, "red cube", new Vector3(layoutCenter.x + layoutHalfX, floorY + 0.27f, layoutCenter.z - layoutRowZ), PaintRed);
        objects[2] = MakeObject(PrimitiveType.Sphere, "blue sphere", new Vector3(layoutCenter.x - layoutHalfX * 0.8f, floorY + 0.27f, layoutCenter.z + layoutFarZ), PaintBlue);
        objects[3] = MakeObject(PrimitiveType.Cube, "blue cube", new Vector3(layoutCenter.x + layoutHalfX * 0.8f, floorY + 0.27f, layoutCenter.z + layoutFarZ), PaintBlue);
        objects[4] = MakeObject(PrimitiveType.Cylinder, "yellow cylinder", new Vector3(layoutCenter.x - layoutHalfX, floorY + 0.48f, layoutCenter.z), PaintYellow);
        objects[4].localScale = new Vector3(0.38f, 0.48f, 0.38f);
        objects[5] = MakeCone("yellow cone", new Vector3(layoutCenter.x + layoutHalfX, floorY + 0.39f, layoutCenter.z), PaintYellow);
        objects[5].localScale = Vector3.one * 1.3f;
        // Save spawn state for /reset
        spawnPositions = new Vector3[objects.Length];
        spawnRotations = new Quaternion[objects.Length];
        for (int i = 0; i < objects.Length; i++) { spawnPositions[i] = objects[i].position; spawnRotations[i] = objects[i].rotation; }
        robotRoot.position = robotStart;
        robotRoot.rotation = Quaternion.Euler(0, 90, 0);
        var cameraObject = new GameObject("Robot POV Camera");
        cameraObject.transform.SetParent(robotRoot, false);
        cameraObject.transform.localPosition = new Vector3(0, 0.5f, 0.28f);
        cameraObject.transform.localRotation = Quaternion.Euler(10, 0, 0);
        robotCamera = cameraObject.AddComponent<Camera>();
        robotCamera.fieldOfView = 73.74f; // 90 degree horizontal FOV at 4:3 aspect
        robotCamera.aspect = 4f / 3f;
        robotCamera.nearClipPlane = 0.03f;
        robotCamera.enabled = false; // Camera imagery is rendered on demand by /look only.
        cameraTexture = new RenderTexture(640, 480, 16);
        cameraPixels = new Texture2D(640, 480, TextureFormat.RGB24, false);
        robotCamera.targetTexture = cameraTexture;
        if (overviewCamera == null)
        {
            var overviewObject = new GameObject("Overview Camera");
            Vector3 warehouseCenter = warehouseBounds.center;
            float cameraHeight = Mathf.Max(warehouseBounds.size.x, warehouseBounds.size.z) * 0.62f;
            overviewObject.transform.position = warehouseCenter + new Vector3(-cameraHeight * 0.45f, cameraHeight, cameraHeight * 0.68f);
            overviewObject.transform.LookAt(warehouseCenter);
            overviewCamera = overviewObject.AddComponent<Camera>();
            overviewCamera.fieldOfView = 56f;
            overviewCamera.nearClipPlane = 0.1f;
            overviewCamera.farClipPlane = 100f;
            overviewCamera.enabled = true;
        }
        var lightObject = new GameObject("Sun");
        lightObject.transform.rotation = Quaternion.Euler(50, -30, 0);
        var sun = lightObject.AddComponent<Light>();
        sun.type = LightType.Directional;
        sun.shadows = LightShadows.Soft;   // contact shadows ground the objects on the floor
        sun.shadowStrength = 0.75f;
        BuildViewCameras();
    }

    private void AttachTurtleBotVisual(Transform parent)
    {
        if (sceneRobotVisualSource == null)
        {
            Debug.LogError("TurtleBot3 visual source 'turtlebot3_burger' was not found in the starting scene.");
            return;
        }

        var visual = Instantiate(sceneRobotVisualSource, parent, false);
        visual.name = "TurtleBot3 Visual";
        visual.SetActive(false);
        foreach (var behaviour in visual.GetComponentsInChildren<MonoBehaviour>(true)) behaviour.enabled = false;
        foreach (var camera in visual.GetComponentsInChildren<Camera>(true)) camera.enabled = false;
        foreach (var audio in visual.GetComponentsInChildren<AudioListener>(true)) audio.enabled = false;
        foreach (var collider in visual.GetComponentsInChildren<Collider>(true)) Destroy(collider);
        foreach (var joint in visual.GetComponentsInChildren<Joint>(true)) Destroy(joint);
        foreach (var body in visual.GetComponentsInChildren<Rigidbody>(true))
        {
            body.isKinematic = true;
            body.useGravity = false;
            body.detectCollisions = false;
        }
        visual.SetActive(true);

        // Keep the imported ROBOTIS meshes, but size their visible bounds to the
        // controller footprint so the robot is easy to see and collisions match it.
        var renderers = visual.GetComponentsInChildren<Renderer>(true);
        if (renderers.Length == 0)
        {
            Debug.LogError("TurtleBot3 visual has no renderers; check the imported prefab meshes.");
            return;
        }
        Bounds bounds = renderers[0].bounds;
        for (int i = 1; i < renderers.Length; i++) bounds.Encapsulate(renderers[i].bounds);
        float footprint = Mathf.Max(bounds.size.x, bounds.size.z);
        if (footprint < 0.001f) return;
        visual.transform.localScale = Vector3.one * (0.78f / footprint);
        bounds = new Bounds();
        bool hasBounds = false;
        foreach (var renderer in renderers)
        {
            if (!renderer.enabled) continue;
            if (!hasBounds) { bounds = renderer.bounds; hasBounds = true; }
            else bounds.Encapsulate(renderer.bounds);
        }
        if (hasBounds) visual.transform.position += Vector3.up * (parent.position.y - bounds.min.y + 0.015f);
    }

    private void RemoveDecorativeWarehouseBlockers()
    {
        // Preserve warehouse props and their collisions outside the robot's work
        // footprint; disable only collisions where the Round 1 task needs clear floor.
        var warehouseScene = SceneManager.GetSceneByName("URPScene");
        if (!warehouseScene.IsValid()) return;
        var playArea = GetRoundOnePlayArea();
        foreach (var root in warehouseScene.GetRootGameObjects())
        foreach (var collider in root.GetComponentsInChildren<Collider>(true))
        {
            string name = collider.gameObject.name;
            if (name.Equals("Floor", StringComparison.OrdinalIgnoreCase)
                || name.StartsWith("Wall_", StringComparison.OrdinalIgnoreCase)
                || name.Contains("Wall", StringComparison.OrdinalIgnoreCase)) continue;
            if (playArea.Intersects(collider.bounds)) collider.enabled = false;
        }
    }

    // Visible props must be solid and hidden ones must not be. The two passes above judge a prop by
    // its collider and by its mesh separately, so a prop on the edge of the cleared area could stay
    // visible with its collider off (the robot would drive through it). Some props have no collider at
    // all. Line every visible prop up with a collider here; walls and the floor are left as they are.
    private void MatchWarehouseColliders()
    {
        var warehouseScene = SceneManager.GetSceneByName("URPScene");
        if (!warehouseScene.IsValid()) return;
        int added = 0, enabled = 0, disabled = 0;
        foreach (var root in warehouseScene.GetRootGameObjects())
        foreach (var renderer in root.GetComponentsInChildren<Renderer>(false))
        {
            if (!(renderer is MeshRenderer)) continue;
            string name = renderer.gameObject.name;
            if (name.Equals("Floor", StringComparison.OrdinalIgnoreCase) || name.Contains("Wall", StringComparison.OrdinalIgnoreCase)) continue;
            var own = renderer.GetComponents<Collider>();
            if (!renderer.enabled)
            {
                foreach (var c in own) if (c.enabled) { c.enabled = false; disabled++; }
                continue;
            }
            foreach (var c in own) if (!c.enabled) { c.enabled = true; enabled++; }
            var b = renderer.bounds;
            bool reachable = b.size.y > 0.08f && b.min.y < floorHeight + 2.2f; // not floor paint, not ceiling fittings
            if (own.Length == 0 && reachable && renderer.GetComponentInParent<Collider>() == null && renderer.GetComponent<MeshFilter>() != null)
            {
                renderer.gameObject.AddComponent<BoxCollider>(); // sized to the mesh automatically
                added++;
            }
        }
        Debug.Log($"Warehouse props: {added} colliders added, {enabled} re-enabled on visible props, {disabled} turned off on hidden props.");
    }

    private Bounds GetRoundOnePlayArea()
    {
        // Enclose the six spaced targets, robot start, drop zone and the routes
        // between them, with a little clearance for the TurtleBot footprint.
        float halfWidth = layoutHalfX + 1.4f;
        float halfDepth = Mathf.Max(layoutEndZ + layoutRowZ, layoutFarZ) + 0.8f;
        return new Bounds(new Vector3(layoutCenter.x, floorHeight + 1.25f, layoutCenter.z),
            new Vector3(halfWidth * 2f, 2.5f, halfDepth * 2f));
    }

    private void ClearRoundOnePlayArea()
    {
        var warehouseScene = SceneManager.GetSceneByName("URPScene");
        if (!warehouseScene.IsValid()) return;

        // Keep the warehouse as the scene's backdrop. Hide only visible props that
        // intrude into the larger target layout and driving lanes.
        var playArea = GetRoundOnePlayArea();
        foreach (var root in warehouseScene.GetRootGameObjects())
        foreach (var renderer in root.GetComponentsInChildren<Renderer>(true))
        {
            string name = renderer.gameObject.name;
            if (name.Equals("Floor", StringComparison.OrdinalIgnoreCase)
                || name.Contains("Wall", StringComparison.OrdinalIgnoreCase)) continue;
            if (playArea.Intersects(renderer.bounds)) renderer.enabled = false;
        }
    }

    private void PlaceWarehouseProp(GameObject prefab, Vector3 position, Quaternion rotation)
    {
        if (prefab == null) return;
        var prop = Instantiate(prefab, position, rotation);
        prop.name = "Warehouse " + prefab.name;
        if (prop.GetComponentInChildren<Collider>() == null)
            prop.AddComponent<BoxCollider>();
    }

    // Slightly glossy, shadow-casting material so shapes read as solid 3D objects, not flat colour.
    private static void Shade(Renderer renderer, Color color, float smoothness = 0.6f)
    {
        var material = renderer.material;
        material.color = color;
        if (material.HasProperty("_BaseColor")) material.SetColor("_BaseColor", color);
        if (material.HasProperty("_Smoothness")) material.SetFloat("_Smoothness", smoothness);
        if (material.HasProperty("_Glossiness")) material.SetFloat("_Glossiness", smoothness);
        renderer.shadowCastingMode = UnityEngine.Rendering.ShadowCastingMode.On;
        renderer.receiveShadows = true;
    }

    // Pure (1,0,0)-style colours clip to one flat tone under light; these keep visible shading.
    private static readonly Color PaintRed = new Color(0.86f, 0.15f, 0.12f);
    private static readonly Color PaintBlue = new Color(0.13f, 0.30f, 0.92f);
    private static readonly Color PaintYellow = new Color(0.98f, 0.79f, 0.08f);

    private Transform MakeObject(PrimitiveType shape, string name, Vector3 position, Color color)
    {
        var item = GameObject.CreatePrimitive(shape);
        item.name = name;
        item.transform.position = position;
        item.transform.localScale = Vector3.one * 0.48f;
        // Cubes sit at an angle so two faces (and their different shading) show from every camera.
        if (shape == PrimitiveType.Cube) item.transform.rotation = Quaternion.Euler(0f, 25f, 0f);
        Shade(item.GetComponent<Renderer>(), color);
        return item.transform;
    }

    private Transform MakeCone(string name, Vector3 position, Color color)
    {
        const int sides = 24;
        var vertices = new Vector3[sides + 2];
        vertices[0] = new Vector3(0, 0.3f, 0);
        vertices[1] = new Vector3(0, -0.3f, 0);
        for (int i = 0; i < sides; i++)
        {
            float angle = i * Mathf.PI * 2f / sides;
            vertices[i + 2] = new Vector3(Mathf.Cos(angle) * 0.22f, -0.3f, Mathf.Sin(angle) * 0.22f);
        }
        var triangles = new int[sides * 6];
        for (int i = 0; i < sides; i++)
        {
            int next = (i + 1) % sides;
            int t = i * 6;
            // Clockwise as seen from outside (Unity front faces): sides face out, base faces down.
            triangles[t] = 0; triangles[t + 1] = next + 2; triangles[t + 2] = i + 2;
            triangles[t + 3] = 1; triangles[t + 4] = i + 2; triangles[t + 5] = next + 2;
        }
        var mesh = new Mesh { name = "Round 1 cone", vertices = vertices, triangles = triangles };
        mesh.RecalculateNormals(); mesh.RecalculateBounds();
        var cone = new GameObject(name);
        cone.transform.position = position;
        cone.AddComponent<MeshFilter>().sharedMesh = mesh;
        // Use the same lit material as the primitives so the cone shades and casts shadows like them.
        var donor = GameObject.CreatePrimitive(PrimitiveType.Cube);
        donor.SetActive(false);
        var coneRenderer = cone.AddComponent<MeshRenderer>();
        coneRenderer.sharedMaterial = donor.GetComponent<Renderer>().sharedMaterial;
        Destroy(donor);
        Shade(coneRenderer, color);
        var collider = cone.AddComponent<MeshCollider>(); collider.sharedMesh = mesh; collider.convex = true;
        return cone.transform;
    }

}



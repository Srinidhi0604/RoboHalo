using System.Collections;
using System.Collections.Generic;
using UnityEngine;

// Procedural recreation of the warehouse robot reference video: an orange lift robot takes boxes
// off pallet stacks and sets them on a moving conveyor; boxes queue on a static end belt, where a
// second (blue) robot carries them to a marked collection area. Isometric follow camera.
// Everything (meshes, textures, robot, animation) is generated here; the scene holds only this component.
// The loader can also be driven over HTTP (WarehouseApi.cs), take typed orders, and the collector sorts by box kind.
public sealed partial class WarehouseDemo : MonoBehaviour
{
    // Look-and-feel knobs: tune these against the reference video in the Inspector.
    public float driveSpeed = 2.2f;
    public float beltSpeed = 0.5f;
    public float viewSize = 3.4f;
    public bool follow = true;
    public int watch; // index into bots: which robot the camera follows
    public string[] robotNames = { "Atlas", "Bolt", "Comet", "Dash" }; // selector 1, collector 1, selector 2, collector 2
    public enum View { Isometric, Top, Perspective, Chase, RobotEye }
    public View cameraView = View.Isometric;
    public Vector3 eyeOffset = new Vector3(0.7f, 1.3f, -1f); // Robot eye camera, relative to the lift platform
    public Vector3 eyeAngles = new Vector3(22f, -14f, 0f);
    public float simSpeed = 1f; // whole-simulation time scale
    public float uiScale = 1f; // extra panel zoom on top of the automatic fit-to-screen scaling
    public float batteryDrain = 0.8f; // percent of charge per metre driven
    public float chargeRate = 12f; // percent per second on the dock
    public float lowBattery = 20f; // robots finish their delivery, then go and charge below this
    public bool infrared; // lights out, seen through a night-vision camera
    public Vector3 cameraAngles = new Vector3(35f, 45f, 0f);
    public Vector3 sunDirection = new Vector3(1f, -1.2f, -0.6f);
    public Color ambient = new Color(0.48f, 0.46f, 0.66f);
    public float sunIntensity = 0.7f; // gamma colour space: higher blows the floor out to white

    private const float Tile = 1.25f, PalletTop = 0.18f, BeltTop = 0.16f, LidRest = 0.9f, BodyTop = 0.82f;
    private const float PickRowZ = 4.2f, BeltZ = -3f, MovingStart = -7.8f, MovingEnd = 7.8f, StaticEnd = 11.7f;
    private const float Spacing = 0.9f, StaticStart = MovingEnd + 0.45f, QueueHead = StaticEnd - 0.6f; // box centres
    private const int Lanes = 3, LaneDepth = 4; // collection area: one lane per box kind, LaneDepth spots, two boxes high
    private const int AnyKind = 3, AnyColor = 4;
    [System.Serializable] private sealed class OrderQuery { public string text; }
    [System.Serializable] private sealed class OrderReply { public string command, source, error; }
    private static readonly Vector3 LoaderDock = new Vector3(-9.2f, 0f, 0.6f), SorterDock = new Vector3(9.9f, 0f, 1.2f);
    private static readonly Vector3 Loader2Dock = new Vector3(8.6f, 0f, 2.3f), Sorter2Dock = new Vector3(9.9f, 0f, -5.4f);
    private const float ZoneSplit = 1.4f; // selector 1 works the pallet row west of this x, selector 2 east of it
    private static readonly string[] BotNames = { "Selector 1", "Collector 1", "Selector 2", "Collector 2" };
    // Cargo comes in three shapes and four colours, so some boxes share a shape and some share a colour.
    private static readonly string[] KindNames = { "crate", "flat carton", "tall carton", "box" };
    private static readonly string[] ColorNames = { "red", "blue", "yellow", "brown", "" };
    private static readonly Vector3[] KindSizes = { Vector3.one * 0.8f, new Vector3(0.8f, 0.45f, 0.8f), new Vector3(0.62f, 0.9f, 0.62f) };

    private sealed class Cargo { public Transform t; public Vector3 size; }
    private sealed class Painted { public Material material; public Color color; public Texture texture; }
    private sealed class Bot
    {
        public Transform root, lid, column;
        public readonly Transform[] arms = new Transform[2];
        public float liftY = LidRest, armAngle;
        public Light lamp;
        public string status = "Idle";
        public float battery = 100f;
        public Transform slot, inFlight; // belt slot it has reserved, and the box on its way there
    }

    private Mesh box;
    private Material crateMat, bandMat, plainMat, wood, woodDark, orange, orangeDark, red, dark, beltMat, staticBeltMat, navy, blue, yellow, hub;
    private Transform decor, cargoRoot, collectedRoot, cam;
    private Bot loader, sorter;
    private Bot[] bots; // in BotNames order
    private bool restocking;
    private Light sunLight;
    private readonly List<Painted> tracked = new List<Painted>();
    private readonly Dictionary<Texture, Texture> greyCopies = new Dictionary<Texture, Texture>();
    private Light irLamp;
    private Texture2D vignette;
    private bool paused;
    private Camera view;
    private readonly List<Stack<Cargo>> pickColumns = new List<Stack<Cargo>>();
    private readonly List<Transform> onBelt = new List<Transform>();
    // Belt items a robot is handling (a slot reserved for a landing box, or a box being lifted off):
    // they hold their place, nothing pushes them, and everything behind queues up to them.
    private readonly HashSet<Transform> pinned = new HashSet<Transform>();
    private Vector3 focus, focusVelocity;
    private Coroutine loadRoutine;
    private int loaded, collected, shipped;
    private string orderText = "", orderStatus = "Waiting for an Operations request";
    private int orderKind, orderLeft, orderTotal, orderColor = AnyColor, orderInFlight;
    private readonly Queue<string> pendingOrders = new Queue<string>();
    private bool interpretingOrder;
    private Material[,] cargoMats; // [shape, colour]
    private Material zoneMat;      // green: marks the drop zone (the moving belt's rails)
    private readonly Dictionary<Transform, int> cargoKind = new Dictionary<Transform, int>(), cargoColor = new Dictionary<Transform, int>();
    private const int WallLayer = 30; // room walls: seen by the robot camera, hidden from the operator's overview

    private void Start()
    {
        Application.runInBackground = true;
        QualitySettings.shadows = ShadowQuality.All;
        QualitySettings.shadowResolution = ShadowResolution.VeryHigh;
        QualitySettings.shadowCascades = 4;
        QualitySettings.shadowDistance = 45f;
        QualitySettings.antiAliasing = 4;
        RenderSettings.ambientMode = UnityEngine.Rendering.AmbientMode.Flat;
        RenderSettings.ambientLight = ambient;

        box = BoxMesh();
        MakeMaterials();
        decor = new GameObject("Warehouse").transform;
        BuildFloor();
        BuildLayout();
        MergeStatic();
        BuildWalls();
        tagMat = Track(Glow(null, new Color(1f, 0.1f, 0.6f)));
        codeMat = Track(Mat(Color.white, 0.1f, CodeAtlas()));
        // Robots get their own material copies so the two robots can be told apart from same-coloured fixtures.
        loader = BuildRobot(Track(new Material(orange)), Track(new Material(orangeDark)), Vector3.zero);
        sorter = BuildRobot(Track(new Material(blue)), Track(new Material(navy)), new Vector3(StaticStart, 0f, BeltZ + 2.5f));
        var loader2 = BuildRobot(Track(Mat(new Color(0.95f, 0.78f, 0.12f), 0.45f)), Track(Mat(new Color(0.75f, 0.58f, 0.08f), 0.45f)), new Vector3(5f, 0f, 0f));
        var sorter2 = BuildRobot(Track(Mat(new Color(0.1f, 0.62f, 0.6f), 0.45f)), Track(Mat(new Color(0.05f, 0.38f, 0.4f), 0.45f)), new Vector3(StaticStart, 0f, BeltZ - 2.5f));
        bots = new[] { loader, sorter, loader2, sorter2 };
        foreach (var other in new[] { sorter, loader2, sorter2 }) // so an API-driven selector can't drive through them
        {
            var hull = other.root.gameObject.AddComponent<CapsuleCollider>();
            hull.radius = 0.5f;
            hull.height = 1.1f;
            hull.center = new Vector3(0f, 0.55f, 0f);
        }

        var sun = new GameObject("Sun").AddComponent<Light>();
        sun.type = LightType.Directional;
        sun.color = new Color(1f, 0.94f, 0.84f);
        sun.intensity = sunIntensity;
        sun.shadows = LightShadows.Soft;
        sun.shadowStrength = 0.85f;
        sun.transform.rotation = Quaternion.LookRotation(sunDirection);
        sunLight = sun;

        var camera = new GameObject("Main Camera") { tag = "MainCamera" }.AddComponent<Camera>();
        camera.orthographic = true;
        camera.orthographicSize = viewSize;
        camera.clearFlags = CameraClearFlags.SolidColor;
        camera.backgroundColor = new Color(0.42f, 0.42f, 0.42f);
        camera.farClipPlane = 80f;
        camera.nearClipPlane = 0.05f;
        camera.cullingMask &= ~(1 << WallLayer); // walls would block the overview; the robot camera still sees them
        view = camera;
        QualitySettings.pixelLightCount = 6; // IR illuminator plus four headlights
        irLamp = Spot(camera.transform, Vector3.zero, Quaternion.identity, 60f, 42f, 2f); // falls off toward the frame edges
        vignette = new Texture2D(64, 64, TextureFormat.RGBA32, false);
        for (int y = 0; y < 64; y++)
            for (int x = 0; x < 64; x++)
                vignette.SetPixel(x, y, new Color(0f, 0f, 0f, 0.85f * Mathf.SmoothStep(0.55f, 1.05f, new Vector2(x - 31.5f, y - 31.5f).magnitude / 32f)));
        vignette.Apply();
        cam = camera.transform;
        cam.rotation = Quaternion.Euler(cameraAngles);
        focus = loader.root.position;

        ApplyTheme();
        BuildCargo();
        loadRoutine = StartLoader();
        StartCoroutine(Load(loader2, 8, ZoneSplit, float.PositiveInfinity, Loader2Dock, 0f));
        StartCoroutine(Sort(sorter, 1f, SorterDock, 0f));
        StartCoroutine(Sort(sorter2, -1f, Sorter2Dock, 180f));
        StartApi();
    }

    // Conveyor: boxes ride the moving belt, coast onto the static end belt and queue there, pushed
    // along by the boxes behind them until the head of the queue reaches the end stop.
    private void Update()
    {
        PumpApi();
        float step = beltSpeed * Time.deltaTime;
        beltMat.mainTextureOffset -= new Vector2(step / 10.4f, 0f); // matches the 0.26 m rib spacing set in MakeMaterials
        onBelt.Sort((p, q) => p.position.x.CompareTo(q.position.x));
        for (int i = 0; i < onBelt.Count; i++) // back to front: ride, then push the box ahead
        {
            var item = onBelt[i];
            if (!pinned.Contains(item) && item.position.x < StaticStart) item.position += Vector3.right * step;
            if (i + 1 < onBelt.Count && !pinned.Contains(onBelt[i + 1]) && onBelt[i + 1].position.x < item.position.x + Spacing)
                SetX(onBelt[i + 1], item.position.x + Spacing);
        }
        float limit = QueueHead;
        for (int i = onBelt.Count - 1; i >= 0; i--) // front to back: nothing passes the end stop or the box ahead
        {
            if (onBelt[i].position.x > limit) SetX(onBelt[i], limit);
            limit = onBelt[i].position.x - Spacing;
        }
    }

    private static void SetX(Transform item, float x) { var p = item.position; p.x = x; item.position = p; }

    private void LateUpdate()
    {
        // Under API control the world only advances while an action runs: nothing moves while the agent thinks.
        RecordFrame();
        Time.timeScale = paused || (apiMode && !apiBusy) ? 0f : simSpeed;
        var bot = bots[watch].root;
        if (follow) focus = Vector3.SmoothDamp(focus, bot.position + Vector3.up * 0.5f, ref focusVelocity, 0.6f, Mathf.Infinity, Time.unscaledDeltaTime);
        float ease = 1f - Mathf.Exp(-8f * Time.unscaledDeltaTime);
        view.orthographic = cameraView == View.Isometric || cameraView == View.Top;
        view.orthographicSize = viewSize;
        bool close = cameraView == View.Chase || cameraView == View.RobotEye; // illuminator is metres, not 18 m, from the subject
        irLamp.intensity = close ? 0.7f : 2f;
        irLamp.spotAngle = close ? 95f : 58f;
        if (cameraView == View.RobotEye)
        {
            // Mast camera over the robot's right shoulder, riding on the lift: the carried box stays in
            // the left of the frame and the path ahead in the right. A face-level camera can't see the box on its own roof.
            var lift = bots[watch].lid;
            view.fieldOfView = 75f;
            cam.SetPositionAndRotation(lift.TransformPoint(eyeOffset), bot.rotation * Quaternion.Euler(eyeAngles));
        }
        else if (cameraView == View.Chase) // behind and above the watched robot
        {
            view.fieldOfView = 50f;
            Vector3 seat = bot.position - bot.forward * 4.5f + Vector3.up * 3f;
            cam.position = Vector3.Lerp(cam.position, seat, ease * 0.5f);
            cam.rotation = Quaternion.Slerp(cam.rotation, Quaternion.LookRotation(bot.position + Vector3.up * 0.8f - cam.position), ease);
        }
        else // orbit views around the focus point
        {
            view.fieldOfView = 40f;
            var aim = Quaternion.Euler(cameraView == View.Top ? new Vector3(90f, cameraAngles.y, 0f) : cameraAngles);
            cam.rotation = Quaternion.Slerp(cam.rotation, aim, ease);
            cam.position = focus - cam.forward * (view.orthographic ? 18f : viewSize * 4f);
        }
    }

    // Infrared = the warehouse with its lights out, seen through a night-vision security camera:
    // monochrome image, lit only by the camera's own IR illuminator and the robots' headlights.
    private void ApplyTheme()
    {
        foreach (var t in tracked)
        {
            if (t.material.HasProperty("_Color")) t.material.color = infrared ? Grey(t.color) : t.color;
            t.material.mainTexture = infrared ? Grey(t.texture) : t.texture;
        }
        RenderSettings.ambientLight = infrared ? new Color(0.04f, 0.04f, 0.04f) : ambient;
        RenderSettings.reflectionIntensity = infrared ? 0f : 1f; // no sky reflections tinting the dark blue
        sunLight.enabled = !infrared;
        irLamp.enabled = infrared;
        foreach (var bot in bots) bot.lamp.enabled = infrared;
        view.backgroundColor = infrared ? Color.black : new Color(0.42f, 0.42f, 0.42f);
    }

    private Material Track(Material material) // remember daytime paint so ApplyTheme can swap it out and back
    {
        tracked.Add(new Painted
        {
            material = material, texture = material.mainTexture,
            color = material.HasProperty("_Color") ? material.color : Color.white,
        });
        return material;
    }

    private static Color Grey(Color c) { float g = c.grayscale; return new Color(g, g, g, c.a); }

    private Texture Grey(Texture texture) // monochrome copy, made once per texture
    {
        if (texture == null) return null;
        if (!greyCopies.TryGetValue(texture, out var grey))
        {
            var source = (Texture2D)texture;
            var pixels = source.GetPixels();
            for (int i = 0; i < pixels.Length; i++) pixels[i] = Grey(pixels[i]);
            var copy = new Texture2D(source.width, source.height, TextureFormat.RGBA32, true) { anisoLevel = 8 };
            copy.SetPixels(pixels);
            copy.Apply();
            greyCopies[texture] = grey = copy;
        }
        return grey;
    }

    private string RobotName(int i) => i < robotNames.Length && !string.IsNullOrEmpty(robotNames[i]) ? robotNames[i] : BotNames[i];

    private void OnGUI()
    {
        // Panels are laid out for a 720-pixel-tall screen and scaled up to whatever the view really is,
        // so they stay readable on large or zoomed-out Game views.
        float scale = uiScale * Mathf.Max(1f, Screen.height / 720f);
        GUI.matrix = Matrix4x4.Scale(new Vector3(scale, scale, 1f));
        float width = Screen.width / scale, height = Screen.height / scale;
        DrawCodes(scale);
        for (int i = 0; i < bots.Length; i++) // name tag floating above each robot
        {
            Vector3 point = view.WorldToScreenPoint(bots[i].lid.position + Vector3.up * 1.5f);
            if (point.z > 0f) GUI.Box(new Rect(point.x / scale - 40f, (Screen.height - point.y) / scale - 11f, 80f, 22f), "<b>" + RobotName(i) + "</b>");
        }
        if (infrared) // security-camera dressing
        {
            GUI.DrawTexture(new Rect(0, 0, width, height), vignette);
            GUI.Label(new Rect(width / 2f - 110f, 10, 240, 24), "● REC   IR CAM 01   " + System.DateTime.Now.ToString("HH:mm:ss"));
        }
        GUILayout.BeginArea(new Rect(width - 332, 12, 320, Mathf.Min(600f, height - 24f)), GUI.skin.box);
        float minutes = Mathf.Max(Time.timeSinceLevelLoad / 60f, 0.01f);
        GUILayout.Label("<b>Operations</b>");
        GUILayout.Label("Loaded onto belt: " + loaded);
        GUILayout.Label("Collected: " + collected + "   (" + (collected / minutes).ToString("0.0") + " per min)");
        GUILayout.Label("Batches shipped: " + shipped + "     Belt queue: " + onBelt.Count);
        for (int i = 0; i < bots.Length; i++) GUILayout.Label("<b>" + RobotName(i) + "</b> · " + BotNames[i] + " [" + bots[i].battery.ToString("0") + "%]: " + bots[i].status);
        GUILayout.Label("<b>Ask Atlas and Comet</b> (e.g. fetch a small blue box)");
        GUILayout.BeginHorizontal();
        orderText = GUILayout.TextField(orderText);
        GUI.enabled = !interpretingOrder;
        if (GUILayout.Button("Send", GUILayout.Width(50))) StartCoroutine(InterpretOrder(orderText));
        GUI.enabled = true;
        GUILayout.EndHorizontal();
        GUILayout.Label(orderStatus);
        GUILayout.Label("Requested orders waiting: " + pendingOrders.Count);
        GUILayout.Label(!apiListening ? "API: off (port " + apiPort + " unavailable)" : apiMode ? "API: driving selector 1 (" + apiState + ")" : "API: listening on port " + apiPort);
        if (apiMode && GUILayout.Button("Return selector 1 to autonomous")) LeaveApi();
        DrawPriorityPanel();
        GUILayout.EndArea();

        GUILayout.BeginArea(new Rect(12, 12, 270, Mathf.Min(590f, height - 24f)), GUI.skin.box);
        GUILayout.BeginHorizontal();
        GUILayout.Label("Panel size");
        if (GUILayout.Button("−", GUILayout.Width(40))) uiScale = Mathf.Max(0.6f, uiScale - 0.1f);
        if (GUILayout.Button("+", GUILayout.Width(40))) uiScale = Mathf.Min(3f, uiScale + 0.1f);
        GUILayout.EndHorizontal();
        GUILayout.Label("Simulation speed: " + simSpeed.ToString("0.0") + "x");
        simSpeed = GUILayout.HorizontalSlider(simSpeed, 0.1f, 5f);
        GUILayout.Label("Robot speed: " + driveSpeed.ToString("0.0") + " m/s");
        driveSpeed = GUILayout.HorizontalSlider(driveSpeed, 0.5f, 6f);
        GUILayout.Label("Conveyor speed: " + beltSpeed.ToString("0.0") + " m/s");
        beltSpeed = GUILayout.HorizontalSlider(beltSpeed, 0f, 2f);
        GUILayout.Label("Camera view");
        cameraView = (View)GUILayout.SelectionGrid((int)cameraView, new[] { "Isometric", "Top", "Perspective", "Chase", "Robot eye" }, 3);
        GUILayout.Label("Tilt: " + cameraAngles.x.ToString("0") + "°");
        cameraAngles.x = GUILayout.HorizontalSlider(cameraAngles.x, 15f, 89f);
        GUILayout.Label("Zoom");
        viewSize = GUILayout.HorizontalSlider(viewSize, 1.5f, 10f);
        GUILayout.BeginHorizontal();
        if (GUILayout.Button("⟲ Rotate")) cameraAngles.y -= 45f;
        if (GUILayout.Button("Rotate ⟳")) cameraAngles.y += 45f;
        if (GUILayout.Button("Reset")) { cameraAngles = new Vector3(35f, 45f, 0f); viewSize = 3.4f; cameraView = View.Isometric; }
        GUILayout.EndHorizontal();
        GUILayout.Label("Watch robot");
        var watchLabels = new string[bots.Length];
        for (int i = 0; i < bots.Length; i++) watchLabels[i] = RobotName(i) + " (" + BotNames[i] + ")";
        int picked = GUILayout.SelectionGrid(watch, watchLabels, 2);
        if (picked != watch) { watch = picked; follow = true; } // switching always re-centres on that robot
        follow = GUILayout.Toggle(follow, " Follow robot");
        bool thermal = GUILayout.Toggle(infrared, " Infrared mode");
        if (thermal != infrared) { infrared = thermal; ApplyTheme(); }
        if (GUILayout.Button(paused ? "Resume" : "Pause")) paused = !paused;
        GUILayout.EndArea();
    }

    // ---------- behaviour ----------

    private Coroutine StartLoader() => StartCoroutine(Load(loader, 7, float.NegativeInfinity, ZoneSplit, LoaderDock, 270f));

    // Selector robot: aisle stacks -> moving belt. Each selector owns the stretch of the pallet row
    // between minX and maxX, so the two never cross paths.
    private IEnumerator Load(Bot bot, int seed, float minX, float maxX, Vector3 dock, float dockYaw)
    {
        var rng = new System.Random(seed);
        while (true)
        {
            if (orderLeft == 0 && orderInFlight == 0 && pendingOrders.Count > 0) BeginNextOrder();
            if (orderLeft == 0 && orderInFlight > 0)
            {
                bot.status = "Finishing current order";
                yield return null;
                continue;
            }
            var open = pickColumns.FindAll(c => c.Count > 0 && c.Peek().t.position.x >= minX && c.Peek().t.position.x < maxX);
            if (open.Count == 0)
            {
                bot.status = "Waiting for a matching request";
                if (orderLeft > 0 && orderInFlight == 0 && !restocking && !pickColumns.Exists(c => c.Count > 0)) StartCoroutine(Restock());
                yield return null;
                continue;
            }
            var urgent = orderLeft == 0 ? PriorityColumn(open) : null;
            var wanted = orderLeft > 0 ? open.FindAll(c => Matches(c.Peek().t, orderKind, orderColor))
                : urgent != null ? new List<Stack<Cargo>> { urgent } : new List<Stack<Cargo>>();
            if (wanted.Count == 0)
            {
                bot.status = orderLeft > 0 ? "No matching stock in this aisle" : "Waiting for an Operations request";
                if (orderLeft > 0 && orderInFlight == 0 && !restocking && !pickColumns.Exists(c => c.Count > 0 && Matches(c.Peek().t, orderKind, orderColor))) StartCoroutine(Restock());
                yield return null;
                continue;
            }
            if (bot.battery < lowBattery) yield return Recharge(bot, dock, dockYaw);
            bool ordered = orderLeft > 0;
            var cargo = wanted[rng.Next(wanted.Count)].Pop();
            if (ordered) { orderLeft--; orderInFlight++; }
            cargo.t.SetParent(null, true); // claimed: a restock must not sweep it away
            Vector3 at = cargo.t.position;
            float bottom = at.y - cargo.size.y / 2f;
            var stand = new Vector3(at.x, 0f, at.z - cargo.size.z / 2f - 0.62f);

            bool priority = IsPriority(cargo.t.name);
            if (priority) priorityTaken.Add(cargo.t.name);
            bot.status = priority ? "Fetching priority box " + cargo.t.name
                : urgent != null ? "Clearing " + cargo.t.name + " off a priority box"
                : "Fetching " + cargo.t.name + " (" + Describe(Kind(cargo.t), ColorOf(cargo.t)) + ", order)";
            yield return DriveTo(bot, stand - Vector3.forward * 0.6f);
            yield return Face(bot, 0f);
            yield return Lift(bot, Mathf.Max(LidRest, bottom));
            yield return Glide(bot, stand);
            yield return Grab(bot, cargo.t);
            yield return Glide(bot, stand - Vector3.forward * 0.6f);
            yield return Lift(bot, LidRest);

            bot.status = "Carrying to the belt";
            float dropX = Mathf.Clamp(at.x, MovingStart + 0.6f, 5.5f);
            var dropStand = new Vector3(dropX, 0f, BeltZ + 1.25f);
            yield return DriveTo(bot, dropStand);
            yield return Face(bot, 180f);
            yield return Arms(bot, 0f);
            bot.status = "Waiting for a gap on the belt";
            yield return PlaceOnBelt(cargo.t, dropX, bot);
            if (ordered)
            {
                orderInFlight--;
                bool complete = orderLeft == 0 && orderInFlight == 0;
                orderStatus = (complete ? "Order complete: " : "Order in progress: ") + (orderTotal - orderLeft - orderInFlight) + "/" + orderTotal + " " + Describe(orderKind, orderColor);
                if (complete && pendingOrders.Count > 0) BeginNextOrder();
            }
            yield return Glide(bot, dropStand + Vector3.forward * 0.5f);
        }
    }

    private IEnumerator Restock() // every aisle stack is empty: refill the pallets
    {
        restocking = true;
        yield return new WaitForSeconds(2f);
        BuildCargo();
        restocking = false;
    }

    // Lowers a box onto the moving belt at x. The landing slot is reserved first, so belt traffic queues behind it.
    private IEnumerator PlaceOnBelt(Transform item, float x, Bot bot)
    {
        while (onBelt.Exists(b => Mathf.Abs(b.position.x - x) < Spacing + 0.1f)) yield return null; // wait for a gap
        var slot = bot.slot = new GameObject("Drop slot").transform;
        slot.position = new Vector3(x, 0f, BeltZ);
        pinned.Add(slot);
        onBelt.Add(slot);
        item.SetParent(null, true);
        bot.inFlight = item;
        yield return Slide(item, new Vector3(x, BeltTop + item.localScale.y / 2f, BeltZ), false);
        bot.inFlight = null;
        bot.slot = null;
        pinned.Remove(slot);
        onBelt[onBelt.IndexOf(slot)] = item;
        Destroy(slot.gameObject);
        loaded++;
    }

    private IEnumerator Recharge(Bot bot, Vector3 dock, float yaw) // empty-handed trip to the charging dock
    {
        bot.status = "Low battery: heading to the dock";
        yield return DriveTo(bot, dock);
        yield return Face(bot, yaw);
        while (bot.battery < 100f)
        {
            bot.battery = Mathf.Min(100f, bot.battery + chargeRate * Time.deltaTime);
            bot.status = "Charging";
            yield return null;
        }
    }

    private IEnumerator InterpretOrder(string text)
    {
        if (string.IsNullOrWhiteSpace(text)) { orderStatus = "Type a fetch request first"; yield break; }
        interpretingOrder = true;
        orderStatus = "Understanding request...";
        string payload = JsonUtility.ToJson(new OrderQuery { text = text });
        var request = System.Threading.Tasks.Task.Run(() =>
        {
            try { return RequestInterpretation(payload); }
            catch (System.Exception) { return null; }
        });
        float deadline = Time.realtimeSinceStartup + 15f;
        while (!request.IsCompleted && Time.realtimeSinceStartup < deadline) yield return null;
        if (request.Status == System.Threading.Tasks.TaskStatus.RanToCompletion && !string.IsNullOrEmpty(request.Result))
        {
            var reply = JsonUtility.FromJson<OrderReply>(request.Result);
            if (reply != null && !string.IsNullOrEmpty(reply.command)) PlaceOrder(reply.command, reply.source);
            else orderStatus = reply != null && !string.IsNullOrEmpty(reply.error) ? reply.error : "Request not understood";
        }
        else PlaceOrder(text, "local preset fallback");
        interpretingOrder = false;
    }

    private static string RequestInterpretation(string payload)
    {
        var request = (System.Net.HttpWebRequest)System.Net.WebRequest.Create("http://127.0.0.1:8091/interpret");
        request.Method = "POST";
        request.ContentType = "application/json";
        request.Timeout = 12000;
        byte[] body = System.Text.Encoding.UTF8.GetBytes(payload);
        request.ContentLength = body.Length;
        using (var stream = request.GetRequestStream()) stream.Write(body, 0, body.Length);
        using (var response = request.GetResponse())
        using (var reader = new System.IO.StreamReader(response.GetResponseStream())) return reader.ReadToEnd();
    }

    private void PlaceOrder(string text, string source)
    {
        int kind = ParseKind(text), color = ParseColor(text);
        if (!Says(text, "bring|fetch|pick|collect|deliver|load|grab|get|take|retrieve|hand") || (kind < 0 && color == AnyColor))
        {
            orderStatus = "I can fetch a color and size, such as 'fetch 2 small blue boxes'.";
            return;
        }
        pendingOrders.Enqueue(text);
        if (orderLeft == 0 && orderInFlight == 0) BeginNextOrder();
        else orderStatus = "Queued " + Describe(kind < 0 ? AnyKind : kind, color) + " (" + source + ")";
    }

    private void BeginNextOrder()
    {
        string text = pendingOrders.Dequeue();
        orderTotal = orderLeft = ParseCount(text);
        int kind = ParseKind(text);
        orderKind = kind < 0 ? AnyKind : kind;
        orderColor = ParseColor(text);
        orderStatus = "Order in progress: 0/" + orderTotal + " " + Describe(orderKind, orderColor);
    }

    private static bool Says(string text, string words) => System.Text.RegularExpressions.Regex.IsMatch(text.ToLowerInvariant(), @"\b(" + words + ")");

    // Box shape named in free text: 0 crate (cube), 1 flat carton, 2 tall carton, AnyKind for a generic box, -1 if none.
    // Words are matched from their start, so "tallest" counts as tall but "yellow" does not count as low.
    private static int ParseKind(string text)
    {
        return Says(text, "tall|tallest|large|big|high|narrow") ? 2
            : Says(text, "flat|low|short|wide|small") ? 1
            : Says(text, "crate|cube|square|wood") ? 0
            : Says(text, "carton|cardboard") ? 1
            : Says(text, "box|block|package|parcel|item") ? AnyKind : -1;
    }

    private static int ParseColor(string text)
    {
        for (int c = 0; c < AnyColor; c++) if (Says(text, ColorNames[c] + @"\b")) return c;
        return AnyColor;
    }

    private static readonly string[] NumberWords = { "one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten" };

    private static int ParseCount(string text) // "3 crates", "three crates"; 1 when no number is given
    {
        var number = System.Text.RegularExpressions.Regex.Match(text, @"\d+");
        if (number.Success) return Mathf.Clamp(int.Parse(number.Value), 1, 20);
        string t = " " + text.ToLowerInvariant() + " ";
        for (int i = 0; i < NumberWords.Length; i++) if (t.Contains(" " + NumberWords[i] + " ")) return i + 1;
        return 1;
    }

    private static string Describe(int kind, int color) => (color == AnyColor ? "" : ColorNames[color] + " ") + KindNames[kind];

    private int Kind(Transform item) => cargoKind.TryGetValue(item, out int kind) ? kind : 2;

    private int ColorOf(Transform item) => cargoColor.TryGetValue(item, out int color) ? color : 3;

    private bool Matches(Transform item, int kind, int color) => (kind == AnyKind || Kind(item) == kind) && (color == AnyColor || ColorOf(item) == color);

    // Collector robot: static end belt -> the collection lane for that kind of box. side is +1 for the
    // collector working the pallet side of the belt and -1 for the one on the far side; each has its own lanes.
    private IEnumerator Sort(Bot bot, float side, Vector3 dock, float dockYaw)
    {
        if (collectedRoot == null) collectedRoot = new GameObject("Collected").transform;
        var lanes = new Transform[Lanes];
        var counts = new int[Lanes];
        var heights = new float[Lanes, LaneDepth];
        while (true)
        {
            if (bot.battery < lowBattery) yield return Recharge(bot, dock, dockYaw);
            bot.status = "Waiting at the belt";
            Transform head = null;
            while (head == null) // frontmost box that has come to rest on the static belt
            {
                foreach (var item in onBelt)
                    if (!pinned.Contains(item) && item.position.x >= StaticStart - 0.01f && (head == null || item.position.x > head.position.x)) head = item;
                if (head == null) yield return null;
            }
            int lane = Kind(head);
            bot.status = "Collecting a " + KindNames[lane];
            pinned.Add(head); // the queue no longer pushes it while we drive over and lift it
            yield return DriveTo(bot, new Vector3(head.position.x, 0f, BeltZ + side * 1.25f));
            yield return Face(bot, side > 0f ? 180f : 0f);
            yield return Grab(bot, head);
            onBelt.Remove(head);
            pinned.Remove(head);

            // Each lane is entered from its open west end and filled far spot first, so the robot
            // never has to drive through boxes it has already placed.
            if (lanes[lane] == null) { lanes[lane] = new GameObject(KindNames[lane] + " lane").transform; lanes[lane].SetParent(collectedRoot, false); }
            int spot = counts[lane] / 2;
            var at = AreaSpot(LaneDepth - 1 - spot, lane, side);
            var entry = new Vector3(StaticEnd + 0.5f, 0f, at.z);
            float height = head.localScale.y;
            bot.status = "Stacking in the " + KindNames[lane] + " lane";
            yield return DriveTo(bot, entry);
            yield return DriveTo(bot, at - Vector3.right * 1.25f);
            yield return Face(bot, 90f);
            yield return Lift(bot, Mathf.Max(LidRest, heights[lane, spot]));
            yield return Arms(bot, 0f);
            head.SetParent(lanes[lane], true);
            yield return Slide(head, new Vector3(at.x, heights[lane, spot] + height / 2f, at.z), false);
            heights[lane, spot] += height;
            counts[lane]++;
            collected++;
            MarkDelivered(head);
            yield return Glide(bot, entry); // reverse straight back out of the lane
            yield return Lift(bot, LidRest);

            if (counts[lane] < LaneDepth * 2) continue;
            yield return new WaitForSeconds(1f); // lane full: that batch ships out
            Destroy(lanes[lane].gameObject);
            lanes[lane] = null;
            counts[lane] = 0;
            for (int i = 0; i < LaneDepth; i++) heights[lane, i] = 0f;
            shipped++;
        }
    }

    private static Vector3 AreaSpot(int depth, int lane, float side) => new Vector3(StaticEnd + 1.8f + depth, 0f, BeltZ + side * (2f + lane * 1.3f));

    private IEnumerator Grab(Bot bot, Transform item) // pull a box in front of the robot onto its lid and clasp it
    {
        item.SetParent(bot.lid, true);
        yield return Slide(item, new Vector3(0f, 0.04f + item.localScale.y / 2f, 0f), true);
        yield return Arms(bot, -60f);
    }

    private IEnumerator DriveTo(Bot bot, Vector3 target)
    {
        Vector3 delta = target - bot.root.position;
        if (delta.sqrMagnitude < 1e-4f) yield break;
        yield return Face(bot, Mathf.Atan2(delta.x, delta.z) * Mathf.Rad2Deg);
        yield return Glide(bot, target);
    }

    private IEnumerator Glide(Bot bot, Vector3 target) // straight move without turning, so it also reverses
    {
        while ((bot.root.position - target).sqrMagnitude > 1e-6f)
        {
            Vector3 before = bot.root.position;
            bot.root.position = Vector3.MoveTowards(before, target, driveSpeed * Time.deltaTime);
            bot.battery = Mathf.Max(0f, bot.battery - (bot.root.position - before).magnitude * batteryDrain);
            yield return null;
        }
    }

    private IEnumerator Face(Bot bot, float yaw)
    {
        var goal = Quaternion.Euler(0f, yaw, 0f);
        while (Quaternion.Angle(bot.root.rotation, goal) > 0.1f)
        {
            bot.root.rotation = Quaternion.RotateTowards(bot.root.rotation, goal, driveSpeed * 100f * Time.deltaTime);
            yield return null;
        }
        bot.root.rotation = goal;
    }

    private IEnumerator Lift(Bot bot, float lidTop)
    {
        while (!Mathf.Approximately(bot.liftY, lidTop))
        {
            SetLift(bot, Mathf.MoveTowards(bot.liftY, lidTop, 1.5f * Time.deltaTime));
            yield return null;
        }
    }

    private static void SetLift(Bot bot, float lidTop)
    {
        bot.liftY = lidTop;
        bot.lid.localPosition = new Vector3(0f, lidTop - 0.04f, 0f);
        float height = Mathf.Max(0.001f, lidTop - 0.08f - BodyTop);
        bot.column.localScale = new Vector3(0.4f, height, 0.4f);
        bot.column.localPosition = new Vector3(0f, BodyTop + height / 2f, 0f);
    }

    private IEnumerator Arms(Bot bot, float angle)
    {
        while (!Mathf.Approximately(bot.armAngle, angle))
        {
            bot.armAngle = Mathf.MoveTowards(bot.armAngle, angle, 240f * Time.deltaTime);
            foreach (var arm in bot.arms) arm.localRotation = Quaternion.Euler(bot.armAngle, 0f, 0f);
            yield return null;
        }
    }

    private static IEnumerator Slide(Transform item, Vector3 target, bool local)
    {
        Vector3 from = local ? item.localPosition : item.position;
        Quaternion spin = local ? item.localRotation : item.rotation;
        Quaternion upright = Quaternion.Euler(0f, Mathf.Round(spin.eulerAngles.y / 90f) * 90f, 0f);
        for (float t = 0f; t < 1f; t += Time.deltaTime / 0.45f)
        {
            float k = Mathf.SmoothStep(0f, 1f, t);
            if (local) { item.localPosition = Vector3.Lerp(from, target, k); item.localRotation = Quaternion.Slerp(spin, upright, k); }
            else { item.position = Vector3.Lerp(from, target, k); item.rotation = Quaternion.Slerp(spin, upright, k); }
            yield return null;
        }
        if (local) { item.localPosition = target; item.localRotation = upright; }
        else { item.position = target; item.rotation = upright; }
    }

    // ---------- scene ----------

    private void BuildFloor()
    {
        const float half = 40f;
        var mesh = new Mesh
        {
            vertices = new[] { new Vector3(-half, 0, -half), new Vector3(-half, 0, half), new Vector3(half, 0, half), new Vector3(half, 0, -half) },
            uv = new[] { new Vector2(0, 0), new Vector2(0, 1), new Vector2(1, 1), new Vector2(1, 0) },
            triangles = new[] { 0, 1, 2, 0, 2, 3 },
        };
        mesh.RecalculateNormals();
        var floor = new GameObject("Floor");
        floor.transform.SetParent(decor, false);
        floor.AddComponent<MeshFilter>().sharedMesh = mesh;
        var material = Track(Mat(Color.white, 0.25f, FloorTexture()));
        material.mainTextureScale = Vector2.one * (2f * half / (Tile * 4f)); // texture holds 4x4 tiles
        floor.AddComponent<MeshRenderer>().sharedMaterial = material;
        Solid(new Vector3(0f, -0.05f, 0f), new Vector3(2f * half, 0.1f, 2f * half)); // gives /depth a floor to hit
    }

    private void BuildLayout()
    {
        for (int i = 0; i < 9; i++)
        {
            float x = -9.4f + i * 2.4f;
            Pallet(new Vector3(x, 0, PickRowZ));
            Pallet(new Vector3(x, 0, PickRowZ + 2.4f));
            Bay(new Vector3(x, 0, PickRowZ - 2.3f), 1.5f);
            Bay(new Vector3(x, 0, BeltZ + 1.7f), 1.5f);
            Bay(new Vector3(x, 0, BeltZ - 1.7f), 1.5f);
        }
        Conveyor(MovingStart, MovingEnd, beltMat, true, false);
        Conveyor(MovingEnd, StaticEnd, staticBeltMat, false, true);

        // Charging docks: a green pad to park on and a charger post just beyond it.
        var charge = Track(Mat(new Color(0.3f, 0.8f, 0.45f), 0.3f));
        foreach (var (dock, beyond) in new[] { (LoaderDock, Vector3.left), (SorterDock, Vector3.forward), (Loader2Dock, Vector3.forward), (Sorter2Dock, Vector3.back) })
        {
            var pad = Cube(decor, dock + Vector3.up * 0.002f, new Vector3(1.3f, 0.004f, 1.3f), charge);
            pad.GetComponent<Renderer>().shadowCastingMode = UnityEngine.Rendering.ShadowCastingMode.Off;
            Vector3 post = dock + beyond * 0.95f;
            Cube(decor, post + Vector3.up * 0.45f, new Vector3(0.3f, 0.9f, 0.3f), dark);
            Cube(decor, post + Vector3.up * 0.96f, new Vector3(0.34f, 0.12f, 0.34f), charge);
            Solid(post + Vector3.up * 0.5f, new Vector3(0.3f, 1f, 0.3f));
        }

        // Collection areas, one on each side of the belt: a colour-coded lane per box kind.
        var laneColors = new[] { new Color(0.78f, 0.52f, 0.3f), new Color(0.93f, 0.8f, 0.35f), new Color(0.55f, 0.75f, 0.6f) };
        for (int lane = 0; lane < Lanes; lane++)
        {
            var paint = Track(Mat(laneColors[lane], 0.1f));
            foreach (float side in new[] { 1f, -1f })
            {
                Vector3 middle = (AreaSpot(0, lane, side) + AreaSpot(LaneDepth - 1, lane, side)) / 2f;
                var pad = Cube(decor, middle + Vector3.up * 0.002f, new Vector3(LaneDepth + 0.3f, 0.004f, 1.1f), paint);
                pad.GetComponent<Renderer>().shadowCastingMode = UnityEngine.Rendering.ShadowCastingMode.Off;
            }
        }
        foreach (float side in new[] { 1f, -1f })
            Bay((AreaSpot(0, 0, side) + AreaSpot(LaneDepth - 1, Lanes - 1, side)) / 2f, LaneDepth + 1.2f);
    }

    // Invisible collider for the HTTP-driven loader's collision and depth checks.
    private void Solid(Vector3 center, Vector3 size)
    {
        var solid = new GameObject("Solid");
        solid.transform.SetParent(decor, false);
        solid.transform.position = center;
        solid.AddComponent<BoxCollider>().size = size;
    }

    // Static scenery is ~1,300 little boxes. Baking them into one mesh per material turns that into a
    // handful of draw calls; materials are shared, so the infrared swap still applies.
    private void MergeStatic()
    {
        var groups = new Dictionary<(Material, UnityEngine.Rendering.ShadowCastingMode), List<CombineInstance>>();
        foreach (var filter in decor.GetComponentsInChildren<MeshFilter>())
        {
            if (filter.sharedMesh != box) continue;
            var renderer = filter.GetComponent<MeshRenderer>();
            var key = (renderer.sharedMaterial, renderer.shadowCastingMode);
            if (!groups.TryGetValue(key, out var parts)) groups[key] = parts = new List<CombineInstance>();
            parts.Add(new CombineInstance { mesh = box, transform = filter.transform.localToWorldMatrix });
            Destroy(filter.gameObject);
        }
        foreach (var group in groups)
        {
            var mesh = new Mesh { indexFormat = UnityEngine.Rendering.IndexFormat.UInt32 };
            mesh.CombineMeshes(group.Value.ToArray());
            var merged = new GameObject("Merged scenery");
            merged.transform.SetParent(decor, false);
            merged.AddComponent<MeshFilter>().sharedMesh = mesh;
            var renderer = merged.AddComponent<MeshRenderer>();
            renderer.sharedMaterial = group.Key.Item1;
            renderer.shadowCastingMode = group.Key.Item2;
        }
    }

    private void BuildCargo()
    {
        if (cargoRoot != null) Destroy(cargoRoot.gameObject);
        cargoRoot = new GameObject("Cargo").transform;
        pickColumns.Clear();
        foreach (var gone in new List<Transform>(cargoKind.Keys)) if (gone == null) { cargoKind.Remove(gone); cargoColor.Remove(gone); }
        var rng = new System.Random(3); // fixed seed: the same stacks, and so the same box codes, every run
        var combos = new List<int>();   // shape * 4 + colour, shuffled
        for (int k = 0; k < 12; k++) combos.Insert(rng.Next(combos.Count + 1), k);
        int serial = 0, picks = 0;
        for (int i = 0; i < 9; i++)
            for (int row = 0; row < 2; row++)
                for (int c = 0; c < 4; c++)
                {
                    bool front = c < 2;
                    var basePoint = new Vector3(-9.4f + i * 2.4f + (c % 2 == 0 ? -0.45f : 0.45f), PalletTop, PickRowZ + row * 2.4f + (front ? -0.45f : 0.45f));
                    // Aisle-facing stacks are the ones a robot can reach. Each holds one shape and colour, so
                    // the box on top is always the kind of box that stack shows from the aisle.
                    bool reachable = row == 0 && front && i >= 1 && i <= 7;
                    int combo = reachable ? combos[picks++ % combos.Count] : -1;
                    bool narrow = reachable ? combo / 4 == 2 : rng.Next(4) == 0;
                    int count = row == 0 && front ? rng.Next(1, 3) : rng.Next(2, 4);
                    var stack = new Stack<Cargo>();
                    float y = basePoint.y;
                    for (int level = 0; level < count; level++)
                    {
                        int kind = reachable ? combo / 4 : narrow ? 2 : rng.Next(2);
                        int color = reachable ? combo % 4 : rng.Next(4);
                        Vector3 size = KindSizes[kind];
                        var item = Cube(cargoRoot, new Vector3(basePoint.x, y + size.y / 2f, basePoint.z), size, cargoMats[kind, color]);
                        item.gameObject.AddComponent<BoxCollider>();
                        item.name = "BX-" + (++serial).ToString("000");
                        cargoKind[item] = kind;
                        cargoColor[item] = color;
                        allCargo.Add(item);
                        Stencil(item);
                        stack.Push(new Cargo { t = item, size = size });
                        y += size.y;
                    }
                    if (reachable) pickColumns.Add(stack);
                }
        RefreshTags();
    }

    // One room with four walls around the whole warehouse floor. The walls are on their own layer:
    // the robot camera sees them, the operator's overview camera does not (they would hide the scene).
    private void BuildWalls()
    {
        const float minX = -12.5f, maxX = 18.5f, minZ = -10f, maxZ = 10f, height = 2.6f, thick = 0.2f;
        var paint = Track(Mat(new Color(0.82f, 0.83f, 0.86f), 0.1f));
        var room = new GameObject("Walls").transform;
        void Wall(Vector3 center, Vector3 size)
        {
            var wall = Cube(room, center, size, paint);
            wall.gameObject.layer = WallLayer;
            wall.gameObject.AddComponent<BoxCollider>(); // stops the robot and gives /depth something to hit
        }
        Wall(new Vector3((minX + maxX) / 2f, height / 2f, maxZ), new Vector3(maxX - minX + thick, height, thick));
        Wall(new Vector3((minX + maxX) / 2f, height / 2f, minZ), new Vector3(maxX - minX + thick, height, thick));
        Wall(new Vector3(minX, height / 2f, 0f), new Vector3(thick, height, maxZ - minZ));
        Wall(new Vector3(maxX, height / 2f, 0f), new Vector3(thick, height, maxZ - minZ));
    }

    private void Pallet(Vector3 c)
    {
        Solid(c + Vector3.up * (PalletTop / 2f), new Vector3(1.8f, PalletTop, 1.8f));
        for (int i = 0; i < 5; i++) Cube(decor, c + new Vector3(0, 0.165f, -0.78f + i * 0.39f), new Vector3(1.8f, 0.03f, 0.24f), wood);
        for (int i = -1; i <= 1; i++)
        {
            Cube(decor, c + new Vector3(i * 0.8f, 0.135f, 0), new Vector3(0.2f, 0.03f, 1.8f), wood);
            Cube(decor, c + new Vector3(0, 0.015f, i * 0.8f), new Vector3(1.8f, 0.03f, 0.2f), wood);
            for (int j = -1; j <= 1; j++) Cube(decor, c + new Vector3(i * 0.8f, 0.075f, j * 0.8f), new Vector3(0.2f, 0.09f, 0.2f), woodDark);
        }
    }

    // One belt section along x. End frames only go on the outer ends of the line, so boxes never pass through one.
    private void Conveyor(float fromX, float toX, Material surface, bool frameAtStart, bool frameAtEnd)
    {
        const float width = 1.3f;
        float length = toX - fromX;
        var c = new Vector3((fromX + toX) / 2f, 0f, BeltZ);
        Solid(c + Vector3.up * 0.1f, new Vector3(length, 0.2f, width + 0.1f));
        Cube(decor, c + new Vector3(0, BeltTop - 0.03f, 0), new Vector3(length, 0.06f, width - 0.1f), surface);
        foreach (float side in new[] { -width / 2f, width / 2f })
            Cube(decor, c + new Vector3(0, BeltTop - 0.03f, side), new Vector3(length, 0.1f, 0.07f), surface == beltMat ? zoneMat : orange);
        foreach (float end in new[] { fromX, toX })
        {
            foreach (float side in new[] { -width / 2f, width / 2f })
                Cube(decor, new Vector3(end, 0.1f, BeltZ + side), new Vector3(0.14f, 0.2f, 0.14f), yellow);
            if (end == fromX ? !frameAtStart : !frameAtEnd) continue;
            foreach (float side in new[] { -width / 2f, width / 2f })
                Cube(decor, new Vector3(end, 0.4f, BeltZ + side), new Vector3(0.08f, 0.5f, 0.08f), blue);
            Cube(decor, new Vector3(end, 0.62f, BeltZ), new Vector3(0.08f, 0.08f, width + 0.08f), blue);
            var brace = Cube(decor, new Vector3(end, 0.4f, BeltZ), new Vector3(0.05f, 0.05f, Mathf.Sqrt(width * width + 0.16f)), blue);
            brace.localRotation = Quaternion.Euler(Mathf.Atan2(0.4f, width) * Mathf.Rad2Deg, 0, 0);
        }
    }

    // Dashed floor bay: an L at each corner plus one dash mid-side, as painted in the video.
    private void Bay(Vector3 c, float size)
    {
        float half = size / 2f, dash = size * 0.2f;
        for (int side = 0; side < 4; side++)
        {
            bool alongX = side < 2;
            float edge = side % 2 == 0 ? -half : half;
            foreach (float a in new[] { -half + dash / 2f, 0f, half - dash / 2f })
                Cube(decor, c + (alongX ? new Vector3(a, 0.004f, edge) : new Vector3(edge, 0.004f, a)),
                    alongX ? new Vector3(dash, 0.008f, 0.06f) : new Vector3(0.06f, 0.008f, dash), navy)
                    .GetComponent<Renderer>().shadowCastingMode = UnityEngine.Rendering.ShadowCastingMode.Off;
        }
    }

    private Bot BuildRobot(Material body, Material bodyDark, Vector3 at) // local forward is +z, wheels rest on y = 0
    {
        var bot = new Bot();
        var robot = bot.root = new GameObject("Robot").transform;
        robot.position = at;
        Cube(robot, new Vector3(0, 0.31f, 0), new Vector3(0.78f, 0.26f, 1f), bodyDark);
        Cube(robot, new Vector3(0, 0.46f, 0), new Vector3(0.96f, 0.07f, 1.1f), body);
        Cube(robot, new Vector3(0, 0.655f, 0), new Vector3(0.86f, 0.33f, 1f), body);
        foreach (float side in new[] { -0.44f, 0.44f })
        {
            Cube(robot, new Vector3(side, 0.2f, 0), new Vector3(0.17f, 0.3f, 0.62f), dark);
            foreach (float z in new[] { -0.3f, 0.3f })
            {
                Wheel(robot, new Vector3(side, 0.2f, z), 0.2f, 0.18f, dark);
                Wheel(robot, new Vector3(side, 0.2f, z), 0.11f, 0.19f, hub);
            }
        }
        Cube(robot, new Vector3(0, 0.64f, 0.503f), new Vector3(0.36f, 0.2f, 0.01f), Track(Glow(FaceTexture(), Color.white)));
        Cube(robot, new Vector3(0, 0.6f, -0.503f), new Vector3(0.24f, 0.09f, 0.01f), Track(Glow(null, new Color(1f, 0.25f, 0.2f))));

        bot.lamp = Spot(robot, new Vector3(0f, 0.7f, 0.55f), Quaternion.Euler(20f, 0f, 0f), 9f, 70f, 1.6f);
        bot.column = Cube(robot, Vector3.zero, Vector3.one, dark);
        var lid = bot.lid = new GameObject("Lift").transform;
        lid.SetParent(robot, false);
        Cube(lid, Vector3.zero, new Vector3(0.88f, 0.08f, 1.02f), body);
        foreach (float x in new[] { -0.17f, 0.17f }) Cube(lid, new Vector3(x, 0.041f, 0), new Vector3(0.2f, 0.004f, 0.72f), dark);
        for (int i = 0; i < 2; i++)
        {
            bot.arms[i] = new GameObject("Arm").transform;
            bot.arms[i].SetParent(lid, false);
            bot.arms[i].localPosition = new Vector3(i == 0 ? -0.51f : 0.51f, 0f, -0.3f);
            Cube(bot.arms[i], new Vector3(0, 0, 0.3f), new Vector3(0.13f, 0.17f, 0.62f), red);
        }
        SetLift(bot, LidRest);
        return bot;
    }

    private static Light Spot(Transform parent, Vector3 at, Quaternion aim, float range, float angle, float intensity)
    {
        var lamp = new GameObject("Lamp").AddComponent<Light>();
        lamp.transform.SetParent(parent, false);
        lamp.transform.localPosition = at;
        lamp.transform.localRotation = aim;
        lamp.type = LightType.Spot;
        lamp.range = range;
        lamp.spotAngle = angle;
        lamp.intensity = intensity;
        lamp.enabled = false; // ApplyTheme turns lamps on in infrared mode
        return lamp;
    }

    private static void Wheel(Transform robot, Vector3 at, float radius, float width, Material material)
    {
        var wheel = GameObject.CreatePrimitive(PrimitiveType.Cylinder);
        Destroy(wheel.GetComponent<Collider>());
        wheel.transform.SetParent(robot, false);
        wheel.transform.localPosition = at;
        wheel.transform.localRotation = Quaternion.Euler(0, 0, 90);
        wheel.transform.localScale = new Vector3(radius * 2f, width / 2f, radius * 2f);
        wheel.GetComponent<Renderer>().sharedMaterial = material;
    }

    private Transform Cube(Transform parent, Vector3 localPosition, Vector3 size, Material material)
    {
        var part = new GameObject("part");
        part.transform.SetParent(parent, false);
        part.transform.localPosition = localPosition;
        part.transform.localScale = size;
        part.AddComponent<MeshFilter>().sharedMesh = box;
        part.AddComponent<MeshRenderer>().sharedMaterial = material;
        return part.transform;
    }

    // Unit cube whose side faces map to the left half of the texture and top/bottom to the right half.
    private static Mesh BoxMesh()
    {
        var vertices = new List<Vector3>(); var normals = new List<Vector3>(); var uv = new List<Vector2>(); var triangles = new List<int>();
        void Face(Vector3 normal, Vector3 up, float u0)
        {
            Vector3 right = Vector3.Cross(normal, up);
            int i = vertices.Count;
            for (int k = 0; k < 4; k++)
            {
                float a = k == 0 || k == 3 ? -0.5f : 0.5f, b = k < 2 ? -0.5f : 0.5f;
                vertices.Add(normal * 0.5f + right * a + up * b);
                normals.Add(normal);
                uv.Add(new Vector2(u0 + (a + 0.5f) * 0.5f, b + 0.5f));
            }
            triangles.AddRange(new[] { i, i + 3, i + 2, i, i + 2, i + 1 });
        }
        Face(Vector3.forward, Vector3.up, 0f); Face(Vector3.back, Vector3.up, 0f);
        Face(Vector3.right, Vector3.up, 0f); Face(Vector3.left, Vector3.up, 0f);
        Face(Vector3.up, Vector3.forward, 0.5f); Face(Vector3.down, Vector3.forward, 0.5f);
        var mesh = new Mesh { name = "Box" };
        mesh.SetVertices(vertices); mesh.SetNormals(normals); mesh.SetUVs(0, uv); mesh.SetTriangles(triangles, 0);
        return mesh;
    }

    // ---------- materials and textures ----------

    private static Material Mat(Color color, float gloss = 0.15f, Texture texture = null)
    {
        var material = new Material(Shader.Find("Standard")) { color = color, mainTexture = texture };
        material.SetFloat("_Glossiness", gloss);
        return material;
    }

    private static Material Glow(Texture texture, Color color) // unlit, so the face and tail light read as lit screens
    {
        var shader = Shader.Find(texture != null ? "Unlit/Texture" : "Unlit/Color");
        if (shader == null) return Mat(color, 0.5f, texture);
        return new Material(shader) { color = color, mainTexture = texture };
    }

    private void MakeMaterials()
    {
        orange = Mat(new Color(0.95f, 0.5f, 0.1f), 0.45f);
        orangeDark = Mat(new Color(0.8f, 0.38f, 0.08f), 0.45f);
        red = Mat(new Color(0.82f, 0.2f, 0.15f), 0.45f);
        dark = Mat(new Color(0.17f, 0.16f, 0.16f), 0.2f);
        hub = Mat(new Color(0.4f, 0.37f, 0.33f), 0.2f);
        var belt = new Paint(320, 8, 6); // 40 ribs across the texture so the scroll in Update reads as motion
        belt.Rect(0, 0, 1, 1, new Color(0.34f, 0.32f, 0.35f));
        for (int rib = 0; rib < 40; rib++) belt.Rect(rib / 40f, 0, rib / 40f + 0.004f, 1, new Color(0.27f, 0.25f, 0.28f));
        beltMat = Mat(Color.white, 0.1f, belt.Bake());
        beltMat.mainTextureScale = new Vector2((MovingEnd - MovingStart) / 5.2f, 1f); // top face spans half the texture: 20 ribs per 5.2 m
        staticBeltMat = Mat(new Color(0.8f, 0.8f, 0.8f), 0.1f, beltMat.mainTexture);
        staticBeltMat.mainTextureScale = new Vector2((StaticEnd - MovingEnd) / 5.2f, 1f);
        navy = Mat(new Color(0.14f, 0.2f, 0.38f), 0.1f);
        blue = Mat(new Color(0.16f, 0.3f, 0.7f), 0.4f);
        yellow = Mat(new Color(0.95f, 0.65f, 0.12f), 0.4f);

        var tan = new Color(0.8f, 0.62f, 0.38f); var tanTop = new Color(0.85f, 0.68f, 0.44f); var tape = new Color(0.9f, 0.76f, 0.54f);
        var brown = new Color(0.42f, 0.28f, 0.17f); var label = new Color(0.94f, 0.9f, 0.88f);

        var p = new Paint(512, 256, 1); // cardboard with a dark band
        p.Region(0, 2); p.Rect(0, 0, 1, 1, tan); p.Speckle(0, 0, 1, 1, 0.05f, 2, 2);
        p.Rect(0, 0.36f, 1, 0.52f, brown); p.Icons(0.5f, 0.405f, 0.07f, tan);
        p.Qr(0.3f, 0.74f, 0.22f); p.Rect(0.66f, 0.7f, 0.86f, 0.79f, label); p.Rect(0, 0.94f, 1, 0.95f, brown * 1.6f);
        p.Region(1, 2); p.Rect(0, 0, 1, 1, tanTop); p.Speckle(0, 0, 1, 1, 0.05f, 2, 2);
        p.Rect(0.4f, 0, 0.6f, 1, tape); p.Rect(0.55f, 0.42f, 0.8f, 0.56f, label);
        bandMat = Mat(Color.white, 0.1f, p.Bake());

        p = new Paint(512, 256, 2); // plain tall cardboard
        p.Region(0, 2); p.Rect(0, 0, 1, 1, tan); p.Speckle(0, 0, 1, 1, 0.05f, 2, 2);
        p.Qr(0.5f, 0.58f, 0.28f); p.Icons(0.3f, 0.26f, 0.08f, brown); p.Rect(0.6f, 0.8f, 0.82f, 0.87f, label);
        p.Region(1, 2); p.Rect(0, 0, 1, 1, tanTop); p.Speckle(0, 0, 1, 1, 0.05f, 2, 2);
        p.Rect(0.4f, 0, 0.6f, 1, tape); p.Rect(0.55f, 0.42f, 0.8f, 0.56f, label);
        plainMat = Mat(Color.white, 0.1f, p.Bake());

        var frame = new Color(0.62f, 0.43f, 0.26f); var cork = new Color(0.84f, 0.63f, 0.36f);
        p = new Paint(512, 256, 3); // wooden crate: frame around a cork panel
        for (int region = 0; region < 2; region++)
        {
            p.Region(region, 2); p.Rect(0, 0, 1, 1, frame); p.Speckle(0, 0, 1, 1, 0.08f, 16, 2);
            p.Rect(0.1f, 0.1f, 0.9f, 0.9f, cork); p.Speckle(0.1f, 0.1f, 0.9f, 0.9f, 0.35f, 3, 3);
            p.Rect(0.1f, 0.87f, 0.9f, 0.9f, frame * 0.6f);
        }
        p.Rect(0.55f, 0.6f, 0.85f, 0.72f, label);
        p.Region(0, 2); p.Rect(0.14f, 0.1f, 0.86f, 0.125f, brown * 0.4f); p.Qr(0.5f, 0.55f, 0.24f); p.Icons(0.56f, 0.19f, 0.06f, brown);
        crateMat = Mat(Color.white, 0.1f, p.Bake());

        p = new Paint(128, 64, 4); // pallet wood: horizontal grain
        p.Rect(0, 0, 1, 1, new Color(0.56f, 0.37f, 0.23f)); p.Speckle(0, 0, 1, 1, 0.22f, 32, 2);
        wood = Mat(Color.white, 0.1f, p.Bake());
        woodDark = Mat(new Color(0.75f, 0.75f, 0.75f), 0.1f, wood.mainTexture);

        zoneMat = Mat(new Color(0.2f, 0.8f, 0.35f), 0.4f);
        foreach (var material in new[] { orange, orangeDark, red, dark, hub, beltMat, staticBeltMat, navy, blue, yellow, bandMat, plainMat, crateMat, wood, woodDark, zoneMat })
            Track(material);

        // Paint: the box textures are tan, so each tint is the wanted colour divided by tan.
        var shapes = new[] { crateMat, bandMat, plainMat };
        var tints = new[] { new Color(1.06f, 0.22f, 0.28f), new Color(0.18f, 0.5f, 2.3f), new Color(1.25f, 1.3f, 0.18f), new Color(0.78f, 0.66f, 0.55f) };
        cargoMats = new Material[shapes.Length, tints.Length];
        for (int k = 0; k < shapes.Length; k++)
            for (int c = 0; c < tints.Length; c++)
                cargoMats[k, c] = Track(new Material(shapes[k]) { color = tints[c] });
    }

    private static Texture2D FaceTexture() // dark screen with cyan "O_O" eyes
    {
        var screen = new Color(0.05f, 0.1f, 0.12f); var cyan = new Color(0.2f, 0.95f, 1f);
        var p = new Paint(128, 32, 5);
        p.Region(0, 2); p.Rect(0, 0, 1, 1, screen);
        foreach (float x in new[] { 0.2f, 0.62f })
        {
            p.Rect(x, 0.28f, x + 0.18f, 0.8f, cyan);
            p.Rect(x + 0.05f, 0.42f, x + 0.13f, 0.66f, screen);
        }
        p.Rect(0.43f, 0.28f, 0.57f, 0.38f, cyan);
        return p.Bake();
    }

    private static Texture2D FloorTexture() // 4x4 chamfered concrete tiles with corner diamonds and faint star lines
    {
        const int size = 1024, tile = 256;
        var pixels = new Color[size * size];
        var baseColor = new Color(0.78f, 0.75f, 0.77f); var grout = new Color(0.6f, 0.57f, 0.6f);
        for (int y = 0; y < size; y++)
            for (int x = 0; x < size; x++)
            {
                float u = (x % tile) / (float)tile, v = (y % tile) / (float)tile;
                float du = Mathf.Min(u, 1f - u), dv = Mathf.Min(v, 1f - v);
                float shade = 0.95f + 0.1f * Mathf.PerlinNoise(x * 0.012f, y * 0.012f) + 0.03f * Mathf.PerlinNoise(x * 0.11f, y * 0.11f);
                if (Mathf.Abs(u - v) < 0.005f || Mathf.Abs(u + v - 1f) < 0.005f || Mathf.Abs(u - 0.5f) < 0.004f || Mathf.Abs(v - 0.5f) < 0.004f) shade *= 0.96f;
                if (du + dv < 0.16f) shade *= 0.93f;
                if (Mathf.Abs(du + dv - 0.16f) < 0.008f || du < 0.03f || dv < 0.03f) shade *= 0.95f;
                Color c = du < 0.012f || dv < 0.012f ? grout : baseColor * shade;
                c.a = 1f;
                pixels[y * size + x] = c;
            }
        var texture = new Texture2D(size, size, TextureFormat.RGBA32, true) { anisoLevel = 8 };
        texture.SetPixels(pixels);
        texture.Apply();
        return texture;
    }

    // Tiny painter for texture atlases. Coordinates are 0..1 inside the current region; y is up.
    private sealed class Paint
    {
        private readonly int w, h; private readonly Color[] px; private readonly System.Random rng; private int ox, rw;
        public Paint(int w, int h, int seed) { this.w = w; this.h = h; px = new Color[w * h]; rng = new System.Random(seed); Region(0, 1); }
        public void Region(int index, int count) { rw = w / count; ox = index * rw; }
        private int X(float x) => Mathf.Clamp(Mathf.RoundToInt(x * rw), 0, rw);
        private int Y(float y) => Mathf.Clamp(Mathf.RoundToInt(y * h), 0, h);

        public void Rect(float x0, float y0, float x1, float y1, Color c)
        {
            c.a = 1f;
            for (int y = Y(y0); y < Y(y1); y++) for (int x = X(x0); x < X(x1); x++) px[y * w + ox + x] = c;
        }

        public void Speckle(float x0, float y0, float x1, float y1, float amount, int grainX, int grainY)
        {
            int xb = X(x1), yb = Y(y1);
            for (int y = Y(y0); y < yb; y += grainY)
                for (int x = X(x0); x < xb; x += grainX)
                {
                    float f = 1f + ((float)rng.NextDouble() - 0.5f) * amount;
                    for (int yy = y; yy < Mathf.Min(y + grainY, yb); yy++)
                        for (int xx = x; xx < Mathf.Min(x + grainX, xb); xx++)
                        {
                            Color c = px[yy * w + ox + xx];
                            px[yy * w + ox + xx] = new Color(c.r * f, c.g * f, c.b * f, 1f);
                        }
                }
        }

        public void Outline(float x0, float y0, float x1, float y1, float t, Color c)
        {
            Rect(x0, y0, x1, y0 + t, c); Rect(x0, y1 - t, x1, y1, c); Rect(x0, y0, x0 + t, y1, c); Rect(x1 - t, y0, x1, y1, c);
        }

        public void Qr(float cx, float cy, float s)
        {
            const int n = 11;
            Rect(cx - s / 2f, cy - s / 2f, cx + s / 2f, cy + s / 2f, new Color(0.95f, 0.93f, 0.9f));
            float m = s * 0.84f / n, x0 = cx - m * n / 2f, y0 = cy - m * n / 2f;
            for (int i = 0; i < n; i++)
                for (int j = 0; j < n; j++)
                {
                    bool finder = (i < 3 && j < 3) || (i < 3 && j >= n - 3) || (i >= n - 3 && j >= n - 3);
                    bool centre = (i == 1 || i == n - 2) && (j == 1 || j == n - 2);
                    bool on = finder ? !centre : rng.Next(2) == 0;
                    if (on) Rect(x0 + i * m, y0 + j * m, x0 + (i + 1) * m, y0 + (j + 1) * m, new Color(0.2f, 0.15f, 0.12f));
                }
        }

        public void Icons(float x, float y, float s, Color c)
        {
            for (int k = 0; k < 4; k++) Outline(x + k * s * 1.3f, y, x + k * s * 1.3f + s, y + s, s * 0.15f, c);
        }

        public Texture2D Bake()
        {
            var texture = new Texture2D(w, h, TextureFormat.RGBA32, true) { anisoLevel = 8 };
            texture.SetPixels(px);
            texture.Apply();
            return texture;
        }
    }
}

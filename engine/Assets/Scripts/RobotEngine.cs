using System;
using System.Collections;
using System.Collections.Concurrent;
using System.IO;
using System.Net;
using System.Text;
using System.Threading;
using UnityEngine;
using UnityEngine.SceneManagement;

public sealed class RobotEngine : MonoBehaviour
{
    [Serializable] private sealed class MoveInput { public float distance; }
    [Serializable] private sealed class TurnInput { public float degrees; }
    [Serializable] private sealed class StartInput { public string command; }
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
    private Camera robotCamera;
    private RenderTexture cameraTexture;
    private Texture2D cameraPixels;
    private GameObject sceneRobotVisualSource;
    private Transform held;
    private Transform[] objects;
    private Vector3 zoneCenter = new Vector3(2f, 0f, 2f);
    private string robotState = "Idle";
    private float heading;
    private int retries;
    private string targetName;
    private string outcome;
    private bool busy;
    private bool lastCollided;
    private float lastMoved;
    private string guiCommand = "Bring the round red thing";
    private string guiStatus = "Idle. Ready for command.";
    private Vector3[] spawnPositions;
    private Quaternion[] spawnRotations;
    private const float Radius = 0.22f;
    private const float Reach = 0.5f;

    private void Start()
    {
        Application.runInBackground = true;
        QualitySettings.vSyncCount = 0;
        Application.targetFrameRate = 60;
        sceneRobotVisualSource = GameObject.Find("turtlebot3_burger");
        DisableOtherSceneRoots();
        BuildScene();
        listener = new HttpListener();
        listener.Prefixes.Add("http://127.0.0.1:8080/");
        try
        {
            listener.Start();
            listenerThread = new Thread(Listen) { IsBackground = true };
            listenerThread.Start();
            Debug.Log("RoboHalo engine listening on http://127.0.0.1:8080/");
        }
        catch (Exception error) { Debug.LogError("Could not start RoboHalo HTTP server: " + error.Message); }
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

    private void DisableOtherSceneRoots()
    {
        foreach (var root in SceneManager.GetActiveScene().GetRootGameObjects())
            if (root != gameObject) root.SetActive(false);
    }

    private void Update()
    {
        if (!busy && requests.TryDequeue(out var request)) Handle(request);
    }

    private void OnGUI()
    {
        GUILayout.BeginArea(new Rect(15, 15, 380, 240), GUI.skin.box);
        GUILayout.Label("<b><size=15>RoboHalo Arena Controller</size></b>");
        GUILayout.Space(5);
        GUILayout.Label("Enter Command (or click a preset below):");
        
        guiCommand = GUILayout.TextField(guiCommand);

        GUILayout.Space(5);
        GUILayout.BeginHorizontal();
        GUI.enabled = !busy;
        if (GUILayout.Button("▶ Run Command", GUILayout.Height(28)))
        {
            StartCoroutine(ExecuteCommandSequence(guiCommand));
        }
        GUI.enabled = true;
        if (GUILayout.Button("↺ Reset", GUILayout.Height(28), GUILayout.Width(75)))
        {
            StopAllCoroutines();
            busy = false;
            InternalReset();
        }
        GUILayout.EndHorizontal();

        GUILayout.Space(5);
        GUILayout.Label("Quick Presets:");
        GUILayout.BeginHorizontal();
        GUI.enabled = !busy;
        if (GUILayout.Button("Red Thing"))
        {
            guiCommand = "Bring the round red thing";
            StartCoroutine(ExecuteCommandSequence(guiCommand));
        }
        if (GUILayout.Button("Tallest Object"))
        {
            guiCommand = "Bring the tallest object";
            StartCoroutine(ExecuteCommandSequence(guiCommand));
        }
        if (GUILayout.Button("Blue Cube"))
        {
            guiCommand = "Bring the blue cube";
            StartCoroutine(ExecuteCommandSequence(guiCommand));
        }
        GUI.enabled = true;
        GUILayout.EndHorizontal();

        GUILayout.Space(6);
        string outcomeStr = string.IsNullOrEmpty(outcome) ? "None" : outcome == "pass" ? "<color=green><b>PASS</b></color>" : $"<color=red><b>{outcome}</b></color>";
        GUILayout.Label($"<b>Status:</b> {robotState}  |  <b>Holding:</b> {(held != null ? held.name : "None")}");
        GUILayout.Label($"<b>Outcome:</b> {outcomeStr}");
        if (!string.IsNullOrEmpty(guiStatus))
        {
            GUILayout.Label($"<i>{guiStatus}</i>");
        }
        GUILayout.EndArea();
    }

    private void Handle(HttpListenerContext context)
    {
        string path = context.Request.Url.AbsolutePath;
        string method = context.Request.HttpMethod;
        try
        {
            if (robotState == "Done" && method == "POST" && path != "/reset") { Error(context, 409, "Run finished"); return; }
            if (method == "GET" && path == "/state")
            {
                var p = transform.position;
                Reply(context, new StateOutput { name = robotState, state = robotState, x = p.x, y = p.z, heading = heading, holding = held != null, retry_count = retries, retries = retries, outcome = outcome });
            }
            else if (method == "GET" && path == "/look") ReplyImage(context);
            else if (method == "GET" && path == "/depth")
            {
                var ray = new Ray(robotCamera.transform.position, robotCamera.transform.forward);
                string value = Physics.Raycast(ray, out var hit, 10f) ? hit.distance.ToString("0.###", System.Globalization.CultureInfo.InvariantCulture) : "null";
                ReplyRaw(context, Encoding.UTF8.GetBytes(value), "application/json");
            }
            else if (method == "POST" && path == "/start") StartRun(context);
            else if (method == "POST" && path == "/move")
            {
                var input = JsonUtility.FromJson<MoveInput>(ReadBody(context));
                if (input == null || float.IsNaN(input.distance) || float.IsInfinity(input.distance) || Mathf.Abs(input.distance) > 3f) { Error(context, 400, "Invalid distance"); return; }
                StartCoroutine(Move(context, input.distance));
            }
            else if (method == "POST" && path == "/turn")
            {
                var input = JsonUtility.FromJson<TurnInput>(ReadBody(context));
                if (input == null || float.IsNaN(input.degrees) || float.IsInfinity(input.degrees) || Mathf.Abs(input.degrees) > 180f) { Error(context, 400, "Invalid degrees"); return; }
                StartCoroutine(Turn(context, input.degrees));
            }
            else if (method == "POST" && path == "/pickup") PickUp(context);
            else if (method == "POST" && path == "/drop") Drop(context);
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
            if (Physics.SphereCast(transform.position + Vector3.up * 0.18f, Radius, direction, out _, step + 0.01f, ~0, QueryTriggerInteraction.Ignore))
            {
                collided = true;
                retries++;
                robotState = retries >= 3 ? "Done" : "Blocked";
                if (retries >= 3) outcome = "fail_collision";
                transform.position -= direction * 0.1f;
                break;
            }
            transform.position += direction * step;
            remaining -= step;
            lastMoved += step;
            yield return null;
        }
        lastCollided = collided;
        lastMoved *= sign;
        if (!collided) robotState = held == null ? "Locate" : "Carry";
        busy = false;
    }

    private IEnumerator TurnRoutine(float degrees)
    {
        busy = true;
        float remaining = Mathf.Abs(degrees);
        float sign = Mathf.Sign(degrees);
        while (remaining > 0.001f)
        {
            float step = Mathf.Min(remaining, Time.deltaTime * 90f);
            heading = Mathf.Repeat(heading + sign * step, 360f);
            transform.rotation = Quaternion.Euler(0f, 90f + heading, 0f);
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
        Reply(context, new ActionOutput { success = true, ok = true, heading = heading });
    }

    private bool InternalPickUp()
    {
        robotState = "Pick up";
        if (held != null) { robotState = "Carry"; return false; }
        Transform nearest = null;
        float best = Reach;
        foreach (var item in objects)
        {
            if (item == null || item.parent == transform) continue;
            Vector3 delta = item.position - transform.position;
            delta.y = 0;
            float angle = Vector3.Angle(Forward(), delta);
            if (delta.magnitude < best && angle <= 30f) { nearest = item; best = delta.magnitude; }
        }
        if (nearest == null) { robotState = "Locate"; return false; }
        held = nearest;
        held.SetParent(transform, true);
        held.localPosition = new Vector3(0.35f, 0.25f, 0f);
        var collider = held.GetComponent<Collider>();
        if (collider != null) collider.enabled = false;
        robotState = "Carry";
        return true;
    }

    private bool InternalDrop(out bool inZone)
    {
        robotState = "Drop";
        inZone = false;
        if (held == null) { robotState = "Locate"; return false; }
        Vector3 dropPoint = transform.position + Forward() * 0.4f;
        string droppedName = held.name;
        held.SetParent(null, true);
        held.position = new Vector3(dropPoint.x, 0.16f, dropPoint.z);
        var collider = held.GetComponent<Collider>();
        if (collider != null) collider.enabled = true;
        held = null;
        inZone = Mathf.Abs(dropPoint.x - zoneCenter.x) < 0.5f && Mathf.Abs(dropPoint.z - zoneCenter.z) < 0.5f;
        robotState = "Done";
        outcome = !inZone ? "fail_not_reached" : droppedName == targetName ? "pass" : "fail_wrong_object";
        return true;
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
        var input = JsonUtility.FromJson<StartInput>(ReadBody(context));
        if (input == null || string.IsNullOrWhiteSpace(input.command)) { Error(context, 400, "Command is required"); return; }
        if (held != null || robotState != "Idle") { Error(context, 409, "Run already started"); return; }
        targetName = ResolveTarget(input.command);
        if (targetName == null) { Error(context, 400, "Command does not identify a supported target"); return; }
        retries = 0;
        outcome = null;
        robotState = "Locate";
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
        transform.position = new Vector3(0, 0.08f, 0);
        transform.rotation = Quaternion.Euler(0, 90, 0);
        heading = 0f;
        retries = 0;
        outcome = null;
        targetName = null;
        busy = false;
        robotState = "Idle";
        guiStatus = "Idle. Ready for command.";
    }

    private void ResetScene(HttpListenerContext context)
    {
        InternalReset();
        Reply(context, new ActionOutput { success = true, ok = true });
    }

    private Vector2 GetTargetPos(string name)
    {
        switch (name)
        {
            case "red sphere": return new Vector2(1.6f, -1.6f);
            case "red cube": return new Vector2(-1.6f, -1.6f);
            case "blue sphere": return new Vector2(2.0f, 0.0f);
            case "blue cube": return new Vector2(-2.0f, 0.0f);
            case "yellow cylinder": return new Vector2(-1.6f, 1.6f);
            case "yellow cone": return new Vector2(0.0f, 2.0f);
            default: return Vector2.zero;
        }
    }

    private IEnumerator ExecuteLeg(Vector2 start, Vector2 end, float stopShort)
    {
        float dx = end.x - start.x;
        float dy = end.y - start.y;
        float length = Mathf.Sqrt(dx * dx + dy * dy);
        float desired = Mathf.Repeat(Mathf.Atan2(-dy, dx) * Mathf.Rad2Deg, 360f);
        float distance = Mathf.Max(0f, length - stopShort);
        float turn = (desired - heading + 180f) % 360f - 180f;
        if (Mathf.Abs(turn) > 0.01f)
        {
            yield return StartCoroutine(TurnRoutine(turn));
        }
        while (distance > 0.001f)
        {
            float step = Mathf.Min(1.0f, distance);
            yield return StartCoroutine(MoveRoutine(step));
            if (lastCollided) yield break;
            distance -= step;
        }
    }

    private IEnumerator ExecuteCommandSequence(string cmd)
    {
        InternalReset();
        yield return null;
        targetName = ResolveTarget(cmd);
        if (targetName == null)
        {
            guiStatus = $"Target not found for: '{cmd}'";
            yield break;
        }
        guiStatus = $"Target: {targetName}. Approaching...";
        robotState = "Locate";
        Vector2 targetPos = GetTargetPos(targetName);
        Vector2 zonePos = new Vector2(zoneCenter.x, zoneCenter.z);

        // Leg 1: origin -> pickup point (stop 0.44m short)
        yield return StartCoroutine(ExecuteLeg(Vector2.zero, targetPos, 0.44f));
        if (lastCollided) { guiStatus = "Collided on approach!"; yield break; }

        // Pick up
        yield return new WaitForSeconds(0.1f);
        bool picked = InternalPickUp();
        if (!picked)
        {
            guiStatus = "Pick up failed (not in reach)!";
            yield break;
        }
        guiStatus = $"Picked up {targetName}! Returning to origin...";
        yield return new WaitForSeconds(0.15f);

        // Leg 2: pickup point -> origin
        Vector2 pickupPt = new Vector2(transform.position.x, transform.position.z);
        yield return StartCoroutine(ExecuteLeg(pickupPt, Vector2.zero, 0f));
        if (lastCollided) { guiStatus = "Collided returning to origin!"; yield break; }

        // Leg 3: origin -> drop zone
        guiStatus = "Delivering to drop zone...";
        yield return StartCoroutine(ExecuteLeg(Vector2.zero, zonePos, 0f));
        if (lastCollided) { guiStatus = "Collided delivering to zone!"; yield break; }

        // Drop
        yield return new WaitForSeconds(0.1f);
        bool inZone;
        InternalDrop(out inZone);
        guiStatus = $"Completed! Outcome: {outcome}";
    }

    private string ResolveTarget(string command)
    {
        string text = command.ToLowerInvariant();
        if (text.Contains("tallest")) return "yellow cylinder";
        string color = text.Contains("red") ? "red" : text.Contains("blue") ? "blue" : text.Contains("yellow") ? "yellow" : null;
        string shape = text.Contains("cube") || text.Contains("square") ? "cube" :
            text.Contains("sphere") || text.Contains("round") || text.Contains("ball") ? "sphere" :
            text.Contains("cylinder") ? "cylinder" : text.Contains("cone") ? "cone" : null;
        if (color != null && shape != null) return color + " " + shape;
        if (color != null)
            foreach (var item in objects) if (item.name.StartsWith(color + " ", StringComparison.Ordinal)) return item.name;
        if (shape != null)
            foreach (var item in objects) if (item.name.EndsWith(" " + shape, StringComparison.Ordinal)) return item.name;
        return null;
    }

    private Vector3 Forward() => new Vector3(Mathf.Cos(heading * Mathf.Deg2Rad), 0f, -Mathf.Sin(heading * Mathf.Deg2Rad));

    private void ReplyImage(HttpListenerContext context)
    {
        robotCamera.Render();
        RenderTexture.active = cameraTexture;
        cameraPixels.ReadPixels(new Rect(0, 0, 640, 480), 0, 0);
        cameraPixels.Apply();
        RenderTexture.active = null;
        ReplyRaw(context, cameraPixels.EncodeToJPG(85), "image/jpeg");
    }

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
        Box("Floor", new Vector3(0, -0.1f, 0), new Vector3(6, 0.2f, 6), new Color(0.65f, 0.65f, 0.65f));
        Box("North wall", new Vector3(0, 0.5f, 3), new Vector3(6, 1, 0.1f), Color.white);
        Box("South wall", new Vector3(0, 0.5f, -3), new Vector3(6, 1, 0.1f), Color.white);
        Box("East wall", new Vector3(3, 0.5f, 0), new Vector3(0.1f, 1, 6), Color.white);
        Box("West wall", new Vector3(-3, 0.5f, 0), new Vector3(0.1f, 1, 6), Color.white);
        var zone = Box("Drop zone", new Vector3(zoneCenter.x, 0.01f, zoneCenter.z), new Vector3(1, 0.02f, 1), Color.green);
        zone.GetComponent<Collider>().isTrigger = true;
        objects = new Transform[6];
        objects[0] = MakeObject(PrimitiveType.Sphere, "red sphere", new Vector3(1.6f, 0.18f, -1.6f), Color.red);
        objects[1] = MakeObject(PrimitiveType.Cube, "red cube", new Vector3(-1.6f, 0.18f, -1.6f), Color.red);
        objects[2] = MakeObject(PrimitiveType.Sphere, "blue sphere", new Vector3(2f, 0.18f, 0f), Color.blue);
        objects[3] = MakeObject(PrimitiveType.Cube, "blue cube", new Vector3(-2f, 0.18f, 0f), Color.blue);
        objects[4] = MakeObject(PrimitiveType.Cylinder, "yellow cylinder", new Vector3(-1.6f, 0.35f, 1.6f), Color.yellow);
        objects[4].localScale = new Vector3(0.25f, 0.35f, 0.25f);
        objects[5] = MakeCone("yellow cone", new Vector3(0f, 0.3f, 2f), Color.yellow);
        // Save spawn state for /reset
        spawnPositions = new Vector3[objects.Length];
        spawnRotations = new Quaternion[objects.Length];
        for (int i = 0; i < objects.Length; i++) { spawnPositions[i] = objects[i].position; spawnRotations[i] = objects[i].rotation; }
        transform.position = new Vector3(0, 0.08f, 0);
        transform.rotation = Quaternion.Euler(0, 90, 0);
        var bodyCollider = gameObject.AddComponent<CapsuleCollider>();
        bodyCollider.radius = Radius; bodyCollider.height = 0.44f; bodyCollider.center = new Vector3(0, 0.22f, 0);
        MakeRobotVisual();
        var cameraObject = new GameObject("Robot POV Camera");
        cameraObject.transform.SetParent(transform, false);
        cameraObject.transform.localPosition = new Vector3(0, 0.5f, 0.28f);
        cameraObject.transform.localRotation = Quaternion.Euler(10, 0, 0);
        robotCamera = cameraObject.AddComponent<Camera>();
        robotCamera.fieldOfView = 73.74f; // 90 degree horizontal FOV at 4:3 aspect
        robotCamera.aspect = 4f / 3f;
        robotCamera.nearClipPlane = 0.03f;
        cameraTexture = new RenderTexture(640, 480, 16);
        cameraPixels = new Texture2D(640, 480, TextureFormat.RGB24, false);
        robotCamera.targetTexture = cameraTexture;
        var overviewObject = new GameObject("Overview Camera");
        overviewObject.transform.position = new Vector3(-4, 5, -4);
        overviewObject.transform.LookAt(Vector3.zero);
        overviewObject.AddComponent<Camera>();
        var lightObject = new GameObject("Sun");
        lightObject.transform.rotation = Quaternion.Euler(50, -30, 0);
        lightObject.AddComponent<Light>().type = LightType.Directional;
    }

    private Transform MakeObject(PrimitiveType shape, string name, Vector3 position, Color color)
    {
        var item = GameObject.CreatePrimitive(shape);
        item.name = name;
        item.transform.position = position;
        item.transform.localScale = Vector3.one * 0.3f;
        item.GetComponent<Renderer>().material.color = color;
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
            triangles[t] = 0; triangles[t + 1] = i + 2; triangles[t + 2] = next + 2;
            triangles[t + 3] = 1; triangles[t + 4] = next + 2; triangles[t + 5] = i + 2;
        }
        var mesh = new Mesh { name = "Round 1 cone", vertices = vertices, triangles = triangles };
        mesh.RecalculateNormals(); mesh.RecalculateBounds();
        var cone = new GameObject(name);
        cone.transform.position = position;
        cone.AddComponent<MeshFilter>().sharedMesh = mesh;
        cone.AddComponent<MeshRenderer>().material.color = color;
        var collider = cone.AddComponent<MeshCollider>(); collider.sharedMesh = mesh; collider.convex = true;
        return cone.transform;
    }

    private void MakeRobotVisual()
    {
        var baseMesh = Resources.Load<GameObject>("TurtleBot/burger_base");
        if (baseMesh != null)
        {
            var visual = Instantiate(baseMesh, transform);
            visual.name = "ROBOTIS Burger visual";
            visual.transform.localPosition = Vector3.zero;
            visual.transform.localRotation = Quaternion.identity;
            visual.transform.localScale = Vector3.one * 1.5f;
            foreach (var collider in visual.GetComponentsInChildren<Collider>()) Destroy(collider);
            AddTire("left_tire", new Vector3(-0.1425f, 0.0525f, 0));
            AddTire("right_tire", new Vector3(0.1425f, 0.0525f, 0));
        }
        else if (sceneRobotVisualSource != null)
        {
            var visual = Instantiate(sceneRobotVisualSource, transform);
            visual.name = "ROBOTIS Burger visual";
            visual.transform.localPosition = Vector3.zero;
            visual.transform.localRotation = Quaternion.identity;
            visual.transform.localScale = Vector3.one * 1.5f;
            foreach (var behaviour in visual.GetComponentsInChildren<MonoBehaviour>()) behaviour.enabled = false;
            foreach (var camera in visual.GetComponentsInChildren<Camera>()) camera.enabled = false;
            foreach (var collider in visual.GetComponentsInChildren<Collider>()) Destroy(collider);
            foreach (var body in visual.GetComponentsInChildren<Rigidbody>()) Destroy(body);
        }
        else Box("Robot body fallback", transform.position + Vector3.up * 0.1f, new Vector3(0.28f, 0.2f, 0.25f), Color.black).transform.SetParent(transform, true);
    }

    private void AddTire(string name, Vector3 offset)
    {
        var source = Resources.Load<GameObject>("TurtleBot/" + name);
        if (source == null) return;
        var tire = Instantiate(source, transform);
        tire.name = name;
        tire.transform.localPosition = offset;
        tire.transform.localRotation = Quaternion.Euler(0, 90, 0);
        tire.transform.localScale = Vector3.one * 1.5f;
        foreach (var collider in tire.GetComponentsInChildren<Collider>()) Destroy(collider);
    }
}

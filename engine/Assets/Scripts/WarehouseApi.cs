using System;
using System.Collections;
using System.Collections.Concurrent;
using System.Collections.Generic;
using System.IO;
using System.Net;
using System.Text;
using System.Threading;
using UnityEngine;

// Local HTTP API for the orange loader, with the same contract as RobotEngine (/state /look /depth
// /start /move /turn /pickup /drop /reset). The first POST takes the loader away from its autonomous
// routine; from then on the world only advances while an action runs, so nothing moves while the
// agent is thinking. Responses carry robot state only: never box names, kinds or positions.
public sealed partial class WarehouseDemo
{
    public int apiPort = 8080;

    [Serializable] private sealed class MoveInput { public float distance; }
    [Serializable] private sealed class TurnInput { public float degrees; }
    [Serializable] private sealed class StartInput { public string command; public string record_dir; }
    [Serializable] private sealed class RecordOutput { public bool recording; public int frames, fps, width, height; }
    [Serializable] private sealed class StateOutput
    {
        public string name, state, outcome;
        public float x, y, heading;
        public bool holding;
        public int retry_count, retries;
    }
    [Serializable] private sealed class ActionOutput
    {
        public bool success, collided, in_zone, ok;
        public float moved, heading;
    }

    private const float ApiRadius = 0.5f, ApiReach = 1.6f;

    private readonly ConcurrentQueue<HttpListenerContext> requests = new ConcurrentQueue<HttpListenerContext>();
    private HttpListener listener;
    private Thread listenerThread;
    private Camera povCamera;
    private RenderTexture povTexture;
    private Texture2D povPixels;
    private bool apiMode, apiBusy, apiListening;
    private string apiState = "Idle", apiOutcome;
    private float apiHeading;
    private int apiRetries, apiKind = AnyKind, apiColor = AnyColor, apiNeeded = 1, apiDelivered;
    private string apiBody = "";
    private Cargo apiHeld;

    private void StartApi()
    {
        // Camera contract from the spec: 640x480, 90 degree horizontal FOV, 0.5 m up, tilted 10 degrees down.
        var eye = new GameObject("Robot POV Camera");
        eye.transform.SetParent(loader.root, false);
        eye.transform.localPosition = new Vector3(0f, 0.5f, 0.58f);
        eye.transform.localRotation = Quaternion.Euler(10f, 0f, 0f);
        povCamera = eye.AddComponent<Camera>();
        povCamera.fieldOfView = 73.74f; // 90 degrees horizontal at 4:3
        povCamera.aspect = 4f / 3f;
        povCamera.nearClipPlane = 0.03f;
        povCamera.farClipPlane = 80f;
        povCamera.clearFlags = CameraClearFlags.SolidColor;
        povCamera.enabled = false; // rendered on demand by /look
        povTexture = new RenderTexture(640, 480, 16);
        povPixels = new Texture2D(640, 480, TextureFormat.RGB24, false);
        povCamera.targetTexture = povTexture;

        listener = new HttpListener();
        listener.Prefixes.Add("http://127.0.0.1:" + apiPort + "/");
        try
        {
            listener.Start();
            listenerThread = new Thread(Listen) { IsBackground = true };
            listenerThread.Start();
            apiListening = true;
        }
        catch (Exception error)
        {
            Debug.LogWarning("Warehouse API not started on port " + apiPort + " (is another engine using it?): " + error.Message);
            listener = null;
        }
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

    private void PumpApi()
    {
        if (!apiBusy && requests.TryDequeue(out var request)) HandleApi(request);
    }

    private void HandleApi(HttpListenerContext context)
    {
        string path = context.Request.Url.AbsolutePath;
        string method = context.Request.HttpMethod;
        // Always read the body first: replying with unread request bytes makes the listener reset the connection.
        apiBody = context.Request.HasEntityBody ? ReadBody(context) : "";
        try
        {
            if (method == "POST" && !apiMode) EnterApi();
            if (apiState == "Done" && method == "POST" && path != "/reset" && path != "/start") { Error(context, 409, "Run finished"); return; }
            if (method == "GET" && path == "/state")
            {
                var p = loader.root.position;
                Reply(context, new StateOutput { name = apiState, state = apiState, x = p.x, y = p.z, heading = apiHeading, holding = apiHeld != null, retry_count = apiRetries, retries = apiRetries, outcome = apiOutcome });
            }
            else if (method == "GET" && path == "/look") ReplyImage(context);
            else if (method == "GET" && path == "/depth")
            {
                var ray = new Ray(povCamera.transform.position, povCamera.transform.forward);
                string value = Physics.Raycast(ray, out var hit, 10f) ? hit.distance.ToString("0.###", System.Globalization.CultureInfo.InvariantCulture) : "null";
                ReplyRaw(context, Encoding.UTF8.GetBytes(value), "application/json");
            }
            else if (method == "POST" && path == "/start")
            {
                var input = JsonUtility.FromJson<StartInput>(apiBody);
                if (input == null || string.IsNullOrWhiteSpace(input.command)) { Error(context, 400, "Command is required"); return; }
                if (apiHeld != null || (apiState != "Idle" && apiState != "Done")) { Error(context, 409, "Run already started"); return; }
                // Private scoring target. Every command is accepted, whatever it says, so the reply never
                // reveals what the engine recognises. A command the engine cannot resolve (for example one
                // that needs reasoning) is scored leniently: any box delivered to the zone counts.
                int kind = ParseKind(input.command);
                apiKind = kind < 0 ? AnyKind : kind;
                apiColor = ParseColor(input.command);
                apiNeeded = Mathf.Min(ParseCount(input.command), 5);
                apiDelivered = 0;
                apiRetries = 0;
                apiOutcome = null;
                apiState = "Locate";
                StartRecording(input.record_dir); // state just left Idle: the clip starts here
                Reply(context, new ActionOutput { success = true, ok = true });
            }
            // Requests are only served between actions, when nothing is being recorded, so "recording" is always false here.
            else if (method == "GET" && path == "/record")
                Reply(context, new RecordOutput { frames = recordFrames, fps = ClipFps, width = ClipWidth, height = ClipHeight });
            else if (method == "POST" && path == "/move")
            {
                var input = JsonUtility.FromJson<MoveInput>(apiBody);
                if (input == null || float.IsNaN(input.distance) || float.IsInfinity(input.distance) || Mathf.Abs(input.distance) > 3f) { Error(context, 400, "Invalid distance"); return; }
                StartCoroutine(ApiMove(context, input.distance));
            }
            else if (method == "POST" && path == "/turn")
            {
                var input = JsonUtility.FromJson<TurnInput>(apiBody);
                if (input == null || float.IsNaN(input.degrees) || float.IsInfinity(input.degrees) || Mathf.Abs(input.degrees) > 180f) { Error(context, 400, "Invalid degrees"); return; }
                StartCoroutine(ApiTurn(context, input.degrees));
            }
            else if (method == "POST" && path == "/pickup") StartCoroutine(ApiPickUp(context));
            else if (method == "POST" && path == "/drop") StartCoroutine(ApiDrop(context));
            else if (method == "POST" && path == "/reset")
            {
                ApiReset();
                Reply(context, new ActionOutput { success = true, ok = true });
            }
            else Error(context, 404, "Unknown endpoint");
        }
        catch (Exception error) { Debug.LogException(error); Error(context, 500, "Engine error"); }
    }

    // ---- Phase 2 clip recorder: robot POV at 1280x720, 30 frames per second of robot motion ----
    // Time is frozen while the agent thinks, so recording only while an action runs makes the clip
    // length equal to real robot motion time. Frames go to the folder the agent names in /start.
    private const int ClipWidth = 1280, ClipHeight = 720, ClipFps = 30;
    private Camera clipCamera;
    private RenderTexture clipTexture;
    private Texture2D clipPixels;
    private string recordDir;
    private int recordFrames;
    private float recordClock;

    private void StartRecording(string dir)
    {
        recordDir = string.IsNullOrEmpty(dir) ? null : dir;
        recordFrames = 0;
        recordClock = 0f;
        if (recordDir == null) return;
        Directory.CreateDirectory(recordDir);
        if (clipCamera != null) return;
        // Same mount as the robot camera, 16:9 with the same 90 degree horizontal field of view.
        var eye = new GameObject("Clip Recording Camera");
        eye.transform.SetParent(loader.root, false);
        eye.transform.localPosition = povCamera.transform.localPosition;
        eye.transform.localRotation = povCamera.transform.localRotation;
        clipCamera = eye.AddComponent<Camera>();
        clipCamera.aspect = 16f / 9f;
        clipCamera.fieldOfView = 2f * Mathf.Atan(Mathf.Tan(45f * Mathf.Deg2Rad) / clipCamera.aspect) * Mathf.Rad2Deg;
        clipCamera.nearClipPlane = povCamera.nearClipPlane;
        clipCamera.farClipPlane = povCamera.farClipPlane;
        clipCamera.clearFlags = CameraClearFlags.SolidColor;
        clipCamera.enabled = false; // rendered on demand
        clipTexture = new RenderTexture(ClipWidth, ClipHeight, 24);
        clipPixels = new Texture2D(ClipWidth, ClipHeight, TextureFormat.RGB24, false);
        clipCamera.targetTexture = clipTexture;
    }

    private void RecordFrame() // called every LateUpdate
    {
        if (recordDir == null || !apiBusy) return;
        recordClock += Time.deltaTime;
        int due = Mathf.FloorToInt(recordClock * ClipFps) - recordFrames;
        if (due <= 0) return;
        clipCamera.backgroundColor = view.backgroundColor;
        clipCamera.Render();
        RenderTexture.active = clipTexture;
        clipPixels.ReadPixels(new Rect(0, 0, ClipWidth, ClipHeight), 0, 0);
        clipPixels.Apply();
        RenderTexture.active = null;
        byte[] jpg = clipPixels.EncodeToJPG(90);
        for (int i = 0; i < due; i++) File.WriteAllBytes(Path.Combine(recordDir, "frame_" + (recordFrames++).ToString("D5") + ".jpg"), jpg);
    }

    private void EnterApi()
    {
        apiMode = true;
        StopCoroutine(loadRoutine);
        ApiReset();
    }

    private void LeaveApi() // hand the loader back to its autonomous routine
    {
        apiMode = false;
        ApiReset();
        loader.status = "Idle";
        loadRoutine = StartLoader();
    }

    // Puts the loader home with empty hands and restocks the pallets, whatever it was in the middle of.
    private void ApiReset()
    {
        if (loader.slot != null) { onBelt.Remove(loader.slot); pinned.Remove(loader.slot); Destroy(loader.slot.gameObject); }
        if (loader.inFlight != null) Destroy(loader.inFlight.gameObject);
        loader.slot = loader.inFlight = null;
        foreach (var carried in loader.lid.GetComponentsInChildren<BoxCollider>(true)) Destroy(carried.gameObject);
        apiHeld = null;
        BuildCargo();
        apiHeading = 0f;
        loader.root.SetPositionAndRotation(Vector3.zero, Quaternion.Euler(0f, 90f, 0f));
        SetLift(loader, LidRest);
        loader.armAngle = 0f;
        foreach (var arm in loader.arms) arm.localRotation = Quaternion.identity;
        apiState = "Idle";
        apiOutcome = null;
        apiRetries = 0;
        apiKind = AnyKind;
        apiColor = AnyColor;
        apiNeeded = 1;
        apiDelivered = 0;
        apiBusy = false;
        recordDir = null; // a reset ends any clip
        loader.status = "API control";
    }

    private Vector3 ApiForward() => new Vector3(Mathf.Cos(apiHeading * Mathf.Deg2Rad), 0f, -Mathf.Sin(apiHeading * Mathf.Deg2Rad));

    private IEnumerator ApiMove(HttpListenerContext context, float distance) // 1 m/s of simulated time
    {
        apiBusy = true;
        apiState = apiHeld == null ? "Go to object" : "Carry";
        float remaining = Mathf.Abs(distance), sign = Mathf.Sign(distance), moved = 0f;
        bool collided = false;
        while (remaining > 0.0001f)
        {
            float step = Mathf.Min(remaining, Time.deltaTime);
            Vector3 direction = ApiForward() * sign;
            if (Physics.SphereCast(loader.root.position + Vector3.up * 0.65f, ApiRadius, direction, out _, step + 0.01f, ~0, QueryTriggerInteraction.Ignore))
            {
                collided = true;
                apiRetries++;
                apiState = apiRetries >= 3 ? "Done" : "Blocked";
                if (apiRetries >= 3) apiOutcome = "fail_collision";
                loader.root.position -= direction * 0.1f;
                break;
            }
            loader.root.position += direction * step;
            remaining -= step;
            moved += step;
            yield return null;
        }
        if (!collided) apiState = apiHeld == null ? "Locate" : "Carry";
        apiBusy = false;
        Reply(context, new ActionOutput { success = !collided, ok = !collided, collided = collided, moved = moved * sign });
    }

    private IEnumerator ApiTurn(HttpListenerContext context, float degrees) // 90 degrees per simulated second
    {
        apiBusy = true;
        float remaining = Mathf.Abs(degrees), sign = Mathf.Sign(degrees);
        while (remaining > 0.001f)
        {
            float step = Mathf.Min(remaining, Time.deltaTime * 90f);
            apiHeading = Mathf.Repeat(apiHeading + sign * step, 360f);
            loader.root.rotation = Quaternion.Euler(0f, 90f + apiHeading, 0f);
            remaining -= step;
            yield return null;
        }
        apiState = apiHeld == null ? "Locate" : "Carry";
        apiBusy = false;
        Reply(context, new ActionOutput { success = true, ok = true, heading = apiHeading });
    }

    private IEnumerator ApiPickUp(HttpListenerContext context) // top box of the nearest aisle stack within reach and 30 degrees
    {
        Stack<Cargo> best = null;
        float nearest = ApiReach;
        if (apiHeld == null)
            foreach (var column in pickColumns)
            {
                if (column.Count == 0) continue;
                Vector3 delta = column.Peek().t.position - loader.root.position;
                delta.y = 0f;
                if (delta.magnitude < nearest && Vector3.Angle(ApiForward(), delta) <= 30f) { best = column; nearest = delta.magnitude; }
            }
        if (best == null)
        {
            if (apiHeld == null) apiState = "Locate";
            Reply(context, new ActionOutput());
            yield break;
        }
        apiBusy = true;
        apiState = "Pick up";
        var cargo = best.Pop();
        cargo.t.SetParent(null, true); // claimed: a restock must not sweep it away
        cargo.t.GetComponent<Collider>().enabled = false;
        yield return Lift(loader, Mathf.Max(LidRest, cargo.t.position.y - cargo.size.y / 2f));
        yield return Grab(loader, cargo.t);
        yield return Lift(loader, LidRest);
        apiHeld = cargo;
        apiState = "Carry";
        apiBusy = false;
        Reply(context, new ActionOutput { success = true, ok = true });
    }

    private IEnumerator ApiDrop(HttpListenerContext context) // the moving belt is the drop zone
    {
        if (apiHeld == null)
        {
            apiState = "Locate";
            Reply(context, new ActionOutput());
            yield break;
        }
        apiBusy = true;
        apiState = "Drop";
        Vector3 point = loader.root.position + ApiForward() * 1.25f;
        bool inZone = Mathf.Abs(point.z - BeltZ) < 0.55f && point.x > MovingStart + 0.5f && point.x < MovingEnd - 0.5f;
        var item = apiHeld.t;
        yield return Arms(loader, 0f);
        if (inZone) yield return PlaceOnBelt(item, point.x, loader);
        else
        {
            item.SetParent(cargoRoot, true);
            yield return Slide(item, new Vector3(point.x, apiHeld.size.y / 2f, point.z), false);
        }
        item.GetComponent<Collider>().enabled = true;
        // A command for several boxes ("bring 3 red crates") keeps the run open until the last one lands.
        bool right = Matches(item, apiKind, apiColor);
        if (inZone && right) apiDelivered++;
        bool finished = !inZone || !right || apiDelivered >= apiNeeded;
        if (finished) apiOutcome = !inZone ? "fail_not_reached" : right ? "pass" : "fail_wrong_object";
        apiHeld = null;
        apiState = finished ? "Done" : "Locate";
        apiBusy = false;
        Reply(context, new ActionOutput { success = true, ok = true, in_zone = inZone });
    }

    private void ReplyImage(HttpListenerContext context)
    {
        povCamera.backgroundColor = view.backgroundColor;
        povCamera.Render();
        RenderTexture.active = povTexture;
        povPixels.ReadPixels(new Rect(0, 0, 640, 480), 0, 0);
        povPixels.Apply();
        RenderTexture.active = null;
        ReplyRaw(context, povPixels.EncodeToJPG(85), "image/jpeg");
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
        Time.timeScale = 1f; // the API freezes time between actions; don't leave the editor frozen
        if (listener != null) { listener.Stop(); listener.Close(); listener = null; }
        if (listenerThread != null && listenerThread.IsAlive) listenerThread.Join(1000);
        if (povTexture != null) povTexture.Release();
        if (clipTexture != null) clipTexture.Release();
    }
}

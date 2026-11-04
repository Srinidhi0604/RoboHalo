using System;
using System.Collections;
using System.Collections.Concurrent;
using System.IO;
using System.Net;
using System.Text;
using System.Threading;
using UnityEngine;

public sealed class RobotEngine : MonoBehaviour
{
    [Serializable] private sealed class MoveInput { public float distance; }
    [Serializable] private sealed class TurnInput { public float degrees; }
    [Serializable] private sealed class StateOutput
    {
        public string state;
        public float x;
        public float y;
        public float heading;
        public bool holding;
        public int retry_count;
    }
    [Serializable] private sealed class ActionOutput
    {
        public bool success;
        public bool collided;
        public bool in_zone;
    }

    private readonly ConcurrentQueue<HttpListenerContext> requests = new ConcurrentQueue<HttpListenerContext>();
    private HttpListener listener;
    private Thread listenerThread;
    private Camera robotCamera;
    private RenderTexture cameraTexture;
    private Texture2D cameraPixels;
    private Transform held;
    private Transform[] objects;
    private Vector3 zoneCenter = new Vector3(2f, 0f, 2f);
    private string robotState = "Idle";
    private float heading;
    private int retries;
    private bool busy;
    private const float Radius = 0.16f;
    private const float Reach = 0.5f;

    private void Start()
    {
        BuildScene();
        listener = new HttpListener();
        listener.Prefixes.Add("http://localhost:8080/");
        try
        {
            listener.Start();
            listenerThread = new Thread(Listen) { IsBackground = true };
            listenerThread.Start();
            Debug.Log("RoboHalo engine listening on http://localhost:8080/");
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

    private void Update()
    {
        if (!busy && requests.TryDequeue(out var request)) Handle(request);
    }

    private void Handle(HttpListenerContext context)
    {
        string path = context.Request.Url.AbsolutePath;
        string method = context.Request.HttpMethod;
        try
        {
            if (robotState == "Done" && method == "POST") { Error(context, 409, "Run finished"); return; }
            if (method == "GET" && path == "/state")
            {
                var p = transform.position;
                Reply(context, new StateOutput { state = robotState, x = p.x, y = p.z, heading = heading, holding = held != null, retry_count = retries });
            }
            else if (method == "GET" && path == "/look") ReplyImage(context);
            else if (method == "GET" && path == "/depth")
            {
                var ray = new Ray(robotCamera.transform.position, robotCamera.transform.forward);
                string value = Physics.Raycast(ray, out var hit, 10f) ? hit.distance.ToString("0.###", System.Globalization.CultureInfo.InvariantCulture) : "null";
                ReplyRaw(context, Encoding.UTF8.GetBytes(value), "application/json");
            }
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
            else Error(context, 404, "Unknown endpoint");
        }
        catch (Exception error) { Debug.LogException(error); Error(context, 500, "Engine error"); }
    }

    private IEnumerator Move(HttpListenerContext context, float distance)
    {
        busy = true;
        robotState = held == null ? "Go to object" : "Carry";
        float remaining = Mathf.Abs(distance);
        float sign = Mathf.Sign(distance);
        bool collided = false;
        while (remaining > 0.0001f)
        {
            float step = Mathf.Min(remaining, Time.deltaTime); // 1 m/s
            Vector3 direction = Forward() * sign;
            if (Physics.SphereCast(transform.position + Vector3.up * 0.18f, Radius, direction, out _, step + 0.01f, ~0, QueryTriggerInteraction.Ignore))
            {
                collided = true;
                retries++;
                robotState = retries >= 3 ? "Done" : "Blocked";
                transform.position -= direction * 0.1f;
                break;
            }
            transform.position += direction * step;
            remaining -= step;
            yield return null;
        }
        if (!collided) robotState = held == null ? "Locate" : "Carry";
        Reply(context, new ActionOutput { success = !collided, collided = collided });
        busy = false;
    }

    private IEnumerator Turn(HttpListenerContext context, float degrees)
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
        Reply(context, new ActionOutput { success = true });
        busy = false;
    }

    private void PickUp(HttpListenerContext context)
    {
        if (held != null) { Reply(context, new ActionOutput { success = false }); return; }
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
        if (nearest == null) { Reply(context, new ActionOutput { success = false }); return; }
        held = nearest;
        held.SetParent(transform, true);
        held.localPosition = new Vector3(0.35f, 0.25f, 0f);
        var collider = held.GetComponent<Collider>();
        if (collider != null) collider.enabled = false;
        robotState = "Carry";
        Reply(context, new ActionOutput { success = true });
    }

    private void Drop(HttpListenerContext context)
    {
        if (held == null) { Reply(context, new ActionOutput { success = false }); return; }
        Vector3 dropPoint = transform.position + Forward() * 0.4f;
        held.SetParent(null, true);
        held.position = new Vector3(dropPoint.x, 0.16f, dropPoint.z);
        var collider = held.GetComponent<Collider>();
        if (collider != null) collider.enabled = true;
        held = null;
        bool inZone = Mathf.Abs(dropPoint.x - zoneCenter.x) < 0.5f && Mathf.Abs(dropPoint.z - zoneCenter.z) < 0.5f;
        robotState = "Done";
        Reply(context, new ActionOutput { success = true, in_zone = inZone });
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
        objects = new Transform[5];
        objects[0] = MakeObject(PrimitiveType.Sphere, "red sphere", new Vector3(1.5f, 0.15f, -1f), Color.red);
        objects[1] = MakeObject(PrimitiveType.Cube, "red cube", new Vector3(-1.5f, 0.15f, -1f), Color.red);
        objects[2] = MakeObject(PrimitiveType.Cylinder, "blue cylinder", new Vector3(1.5f, 0.15f, 1f), Color.blue);
        objects[3] = MakeObject(PrimitiveType.Cube, "blue cube", new Vector3(-1.5f, 0.15f, 1f), Color.blue);
        objects[4] = MakeObject(PrimitiveType.Sphere, "yellow sphere", new Vector3(0.6f, 0.15f, 2f), Color.yellow);
        transform.position = new Vector3(0, 0.08f, 0);
        transform.rotation = Quaternion.Euler(0, 90, 0);
        var bodyCollider = gameObject.AddComponent<CapsuleCollider>();
        bodyCollider.radius = Radius; bodyCollider.height = 0.32f; bodyCollider.center = new Vector3(0, 0.18f, 0);
        MakeRobotVisual();
        var cameraObject = new GameObject("Robot POV Camera");
        cameraObject.transform.SetParent(transform, false);
        cameraObject.transform.localPosition = new Vector3(0, 0.5f, 0.18f);
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

    private void MakeRobotVisual()
    {
        var baseMesh = Resources.Load<GameObject>("TurtleBot/burger_base");
        if (baseMesh != null)
        {
            var visual = Instantiate(baseMesh, transform);
            visual.name = "ROBOTIS Burger visual";
            visual.transform.localPosition = Vector3.zero;
            visual.transform.localRotation = Quaternion.identity;
            foreach (var collider in visual.GetComponentsInChildren<Collider>()) Destroy(collider);
            AddTire("left_tire", new Vector3(-0.095f, 0.035f, 0));
            AddTire("right_tire", new Vector3(0.095f, 0.035f, 0));
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
        foreach (var collider in tire.GetComponentsInChildren<Collider>()) Destroy(collider);
    }
}

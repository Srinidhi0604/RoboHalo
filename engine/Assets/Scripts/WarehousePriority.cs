using System;
using System.Collections.Generic;
using System.IO;
using UnityEngine;

// Box codes and priority deliveries. Every box carries a code (BX-001 ...). A CSV lists the codes
// that must reach the checkpoint (the collection area) first, in rank order: the loader goes for
// those before anything else, clearing whatever is stacked on top of them.
public sealed partial class WarehouseDemo
{
    public string csvPath = "demo.csv"; // relative paths are resolved against the project folder
    public bool showCodes;

    private readonly List<string> priorityIds = new List<string>(); // rank order
    private readonly HashSet<string> priorityDone = new HashSet<string>();
    private readonly HashSet<string> priorityTaken = new HashSet<string>(); // picked up, on its way to the checkpoint
    private readonly List<Transform> allCargo = new List<Transform>();
    private string priorityNote = "No priority list loaded";
    private Material tagMat;

    private string CsvFullPath => Path.IsPathRooted(csvPath) ? csvPath : Path.Combine(Application.dataPath, "..", csvPath);

    // CSV format: first column is the box code; a header row and blank lines are ignored.
    private void ImportCsv()
    {
        try
        {
            var ids = new List<string>();
            foreach (string line in File.ReadAllLines(CsvFullPath))
            {
                string id = line.Split(',')[0].Trim().Trim('"').ToUpperInvariant();
                if (id.Length == 0 || id == "BOX_ID" || ids.Contains(id)) continue;
                ids.Add(id);
            }
            priorityIds.Clear();
            priorityIds.AddRange(ids);
            priorityDone.Clear();
            priorityTaken.Clear();
            RefreshTags();
            int known = ids.FindAll(id => allCargo.Exists(c => c != null && c.name == id)).Count;
            priorityNote = "Loaded " + ids.Count + " priority boxes" + (known < ids.Count ? " (" + (ids.Count - known) + " codes not in the warehouse)" : "");
        }
        catch (Exception error) { priorityNote = "Could not read " + csvPath + ": " + error.Message; }
    }

    // Writes a sample list: ten codes the loader can actually reach, in a fixed shuffled order.
    private void WriteDemoCsv()
    {
        var reachable = new List<string>();
        foreach (var column in pickColumns)
            foreach (var cargo in column) reachable.Add(cargo.t.name);
        var rng = new System.Random(11);
        var lines = new List<string> { "box_id,priority" };
        for (int rank = 1; rank <= 10 && reachable.Count > 0; rank++)
        {
            int pick = rng.Next(reachable.Count);
            lines.Add(reachable[pick] + "," + rank);
            reachable.RemoveAt(pick);
        }
        try
        {
            File.WriteAllLines(CsvFullPath, lines);
            priorityNote = "Wrote " + (lines.Count - 1) + " codes to " + csvPath;
        }
        catch (Exception error) { priorityNote = "Could not write " + csvPath + ": " + error.Message; }
    }

    // The aisle stack holding the highest-ranked priority box that is still on the pallets, or null.
    private Stack<Cargo> PriorityColumn(List<Stack<Cargo>> open)
    {
        foreach (string id in priorityIds)
        {
            if (priorityDone.Contains(id) || priorityTaken.Contains(id)) continue; // a restocked copy is not fetched twice
            foreach (var column in open)
                foreach (var cargo in column)
                    if (cargo.t.name == id) return column;
        }
        return null;
    }

    private bool IsPriority(string id) => priorityIds.Contains(id) && !priorityDone.Contains(id);

    private void MarkDelivered(Transform item) // called when the collector sets a box down at the checkpoint
    {
        if (!IsPriority(item.name)) return;
        priorityDone.Add(item.name);
        var tag = item.Find("Tag");
        if (tag != null) Destroy(tag.gameObject);
    }

    // Pink marker plate on top of every priority box that has not reached the checkpoint yet.
    private void RefreshTags()
    {
        allCargo.RemoveAll(c => c == null);
        foreach (var item in allCargo)
        {
            var tag = item.Find("Tag");
            bool wanted = IsPriority(item.name) && (tag != null || !priorityTaken.Contains(item.name));
            if (wanted && tag == null)
            {
                Vector3 s = item.localScale; // the plate is a child of a scaled box, so undo that scale
                tag = Cube(item, new Vector3(0f, 0.5f + 0.02f / s.y, 0f), new Vector3(0.3f / s.x, 0.04f / s.y, 0.3f / s.z), tagMat); // small enough to leave the printed code visible
                tag.name = "Tag";
            }
            else if (!wanted && tag != null) Destroy(tag.gameObject);
        }
    }

    // ---------- codes stencilled on the boxes ----------

    private const string Glyphs = "0123456789BX-";
    private static readonly string[] GlyphRows = // 3x5 pixel font, rows top to bottom
    {
        "111101101101111", "010110010010111", "111001111100111", "111001111001111", "101101111001001",
        "111100111001111", "111100111101111", "111001001001001", "111101111101111", "111101111001111",
        "110101110101110", "101101010101101", "000000111000000",
    };
    private Material codeMat;
    private readonly Dictionary<string, Mesh> codeMeshes = new Dictionary<string, Mesh>(); // codes repeat on restock

    private static Texture2D CodeAtlas() // white label strip, one 5x7-cell glyph per character
    {
        const int cell = 8, w = 5 * cell, h = 7 * cell;
        var texture = new Texture2D(Glyphs.Length * w, h, TextureFormat.RGBA32, true) { anisoLevel = 8 };
        for (int g = 0; g < Glyphs.Length; g++)
            for (int y = 0; y < h; y++)
                for (int x = 0; x < w; x++)
                {
                    int cx = x / cell - 1, row = 5 - y / cell;
                    bool ink = cx >= 0 && cx < 3 && row >= 0 && row < 5 && GlyphRows[g][row * 3 + cx] == '1';
                    texture.SetPixel(g * w + x, y, ink ? new Color(0.1f, 0.08f, 0.08f) : new Color(0.95f, 0.94f, 0.9f));
                }
        texture.Apply();
        return texture;
    }

    // Label on the top (near the aisle edge) and on the aisle-facing side, in the box's own unit-cube space.
    private static Mesh CodeMesh(string code)
    {
        var vertices = new List<Vector3>(); var normals = new List<Vector3>(); var uv = new List<Vector2>(); var triangles = new List<int>();
        float glyph = 0.84f / code.Length;
        for (int i = 0; i < code.Length; i++)
        {
            int g = Mathf.Max(0, Glyphs.IndexOf(code[i]));
            float x0 = -0.42f + i * glyph, x1 = x0 + glyph, u0 = g / (float)Glyphs.Length, u1 = (g + 1) / (float)Glyphs.Length;
            foreach (bool top in new[] { true, false })
            {
                int n = vertices.Count;
                if (top) vertices.AddRange(new[] { new Vector3(x0, 0.503f, -0.47f), new Vector3(x0, 0.503f, -0.27f), new Vector3(x1, 0.503f, -0.27f), new Vector3(x1, 0.503f, -0.47f) });
                else vertices.AddRange(new[] { new Vector3(x0, 0.18f, -0.503f), new Vector3(x0, 0.42f, -0.503f), new Vector3(x1, 0.42f, -0.503f), new Vector3(x1, 0.18f, -0.503f) });
                for (int k = 0; k < 4; k++) normals.Add(top ? Vector3.up : Vector3.back);
                uv.AddRange(new[] { new Vector2(u0, 0f), new Vector2(u0, 1f), new Vector2(u1, 1f), new Vector2(u1, 0f) });
                triangles.AddRange(new[] { n, n + 1, n + 2, n, n + 2, n + 3 });
            }
        }
        var mesh = new Mesh { name = code };
        mesh.SetVertices(vertices); mesh.SetNormals(normals); mesh.SetUVs(0, uv); mesh.SetTriangles(triangles, 0);
        return mesh;
    }

    private void Stencil(Transform item) // prints the box's code on it
    {
        if (!codeMeshes.TryGetValue(item.name, out var mesh)) codeMeshes[item.name] = mesh = CodeMesh(item.name);
        var label = new GameObject("Code");
        label.transform.SetParent(item, false);
        label.AddComponent<MeshFilter>().sharedMesh = mesh;
        var renderer = label.AddComponent<MeshRenderer>();
        renderer.sharedMaterial = codeMat;
        renderer.shadowCastingMode = UnityEngine.Rendering.ShadowCastingMode.Off;
    }

    private void DrawPriorityPanel()
    {
        int total = priorityIds.Count;
        GUILayout.Label("<b>Priority deliveries</b>" + (total > 0 ? "   " + priorityDone.Count + "/" + total + " at checkpoint" : ""));
        if (total > 0)
        {
            var list = new System.Text.StringBuilder();
            foreach (string id in priorityIds) list.Append(priorityDone.Contains(id) ? "<color=#60e080>✓" + id + "</color>  " : id + "  ");
            GUILayout.Label(list.ToString());
        }
        GUILayout.BeginHorizontal();
        GUILayout.Label("CSV", GUILayout.Width(30));
        csvPath = GUILayout.TextField(csvPath);
        GUILayout.EndHorizontal();
        GUILayout.BeginHorizontal();
        if (GUILayout.Button("Import CSV")) ImportCsv();
#if UNITY_EDITOR
        if (GUILayout.Button("Browse…"))
        {
            string chosen = UnityEditor.EditorUtility.OpenFilePanel("Priority list", Path.GetDirectoryName(CsvFullPath), "csv");
            if (!string.IsNullOrEmpty(chosen)) { csvPath = chosen; ImportCsv(); }
        }
#endif
        if (GUILayout.Button("Write demo.csv")) { csvPath = "demo.csv"; WriteDemoCsv(); }
        GUILayout.EndHorizontal();
        GUILayout.Label(priorityNote);
        showCodes = GUILayout.Toggle(showCodes, " Show every box code");
    }

    // Floating code labels: always for pending priority boxes, for every box when showCodes is on.
    private void DrawCodes(float scale)
    {
        allCargo.RemoveAll(c => c == null);
        foreach (var item in allCargo)
        {
            bool urgent = item.Find("Tag") != null;
            if (!urgent && !showCodes) continue;
            Vector3 point = view.WorldToScreenPoint(item.position + Vector3.up * (item.lossyScale.y / 2f + 0.2f));
            if (point.z <= 0f) continue;
            var spot = new Rect(point.x / scale - 40f, (Screen.height - point.y) / scale - 10f, 90f, 22f);
            GUI.Label(spot, urgent ? "<color=#ff50b0><b>" + item.name + "</b></color>" : item.name);
        }
    }
}

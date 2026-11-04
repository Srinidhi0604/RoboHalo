using UnityEditor;
using UnityEditor.SceneManagement;
using UnityEngine;

public static class BuildScene
{
    [MenuItem("RoboHalo/Create Demo Scene")]
    public static void CreateDemoScene()
    {
        var scene = EditorSceneManager.NewScene(NewSceneSetup.EmptyScene, NewSceneMode.Single);
        new GameObject("RoboHalo Engine").AddComponent<RobotEngine>();
        System.IO.Directory.CreateDirectory("Assets/Scenes");
        EditorSceneManager.SaveScene(scene, "Assets/Scenes/RoboHalo.unity");
        EditorBuildSettings.scenes = new[] { new EditorBuildSettingsScene("Assets/Scenes/RoboHalo.unity", true) };
        Debug.Log("RoboHalo scene created. Press Play to start the local engine API.");
    }

    [MenuItem("RoboHalo/Build Windows Player")]
    public static void BuildWindowsPlayer()
    {
        if (!System.IO.File.Exists("Assets/Scenes/RoboHalo.unity")) CreateDemoScene();
        System.IO.Directory.CreateDirectory("Build");
        var report = UnityEditor.BuildPipeline.BuildPlayer(
            new[] { "Assets/Scenes/RoboHalo.unity" },
            "Build/RoboHalo.exe",
            BuildTarget.StandaloneWindows64,
            BuildOptions.None);
        if (report.summary.result != UnityEditor.Build.Reporting.BuildResult.Succeeded)
            throw new System.Exception("Unity player build failed: " + report.summary.result);
    }
}

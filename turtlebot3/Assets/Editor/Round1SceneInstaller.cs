using UnityEditor;
using UnityEditor.SceneManagement;
using UnityEngine;
using UnityEngine.SceneManagement;

public static class Round1SceneInstaller
{
    private const string EngineName = "RoboHalo Round 1 Engine";

    [MenuItem("RoboHalo/Install Round 1 Into Current Scene")]
    public static void Install()
    {
        var scene = SceneManager.GetActiveScene();
        if (!scene.IsValid() || string.IsNullOrEmpty(scene.path))
            throw new System.InvalidOperationException("Open Tbot3SampleEnv before installing Round 1.");

        GameObject engine = null;
        foreach (var root in scene.GetRootGameObjects())
            if (root.name == EngineName) engine = root;

        if (engine == null)
        {
            engine = new GameObject(EngineName);
            engine.AddComponent<RobotEngine>();
            EditorSceneManager.MarkSceneDirty(scene);
            EditorSceneManager.SaveScene(scene);
        }

        Selection.activeGameObject = engine;
        Debug.Log("Round 1 installed in " + scene.path + ". Press Play, then run agent.manual_agent from E:\\ansu-hack.");
    }
}

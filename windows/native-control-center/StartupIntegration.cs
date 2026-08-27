using System;
using System.Diagnostics;
using System.IO;
using Microsoft.Win32;

namespace AshareAI.Startup
{
    internal static class StartupEntry
    {
        private const string TaskName = "AshareAI Native Control Center";
        private const string StartupScriptName = "AshareAI.NativeControlCenter.Startup.cmd";
        private const string RunKey = @"Software\Microsoft\Windows\CurrentVersion\Run";
        private const string RunValueName = "AshareAI Native Control Center";

        private static string StartupScriptPath(string executable)
        {
            return Path.Combine(Path.GetDirectoryName(executable), StartupScriptName);
        }

        public static bool IsEnabled(string executable)
        {
            try
            {
                using (var key = Registry.CurrentUser.OpenSubKey(RunKey, false))
                {
                    var value = key == null ? null : key.GetValue(RunValueName) as string;
                    return !String.IsNullOrWhiteSpace(value) &&
                        value.StartsWith(Quote(executable), StringComparison.OrdinalIgnoreCase);
                }
            }
            catch { return false; }
        }

        public static void SetEnabled(string executable, string arguments, bool enabled)
        {
            var script = StartupScriptPath(executable);
            if (!enabled)
            {
                try
                {
                    using (var key = Registry.CurrentUser.OpenSubKey(RunKey, true))
                    {
                        if (key != null) key.DeleteValue(RunValueName, false);
                    }
                }
                catch { }
                RemoveLegacyTask();
                DeleteIfExists(script);
                return;
            }

            using (var key = Registry.CurrentUser.CreateSubKey(RunKey))
            {
                if (key == null) throw new InvalidOperationException("无法打开 Windows 当前用户启动项。");
                key.SetValue(
                    RunValueName,
                    BuildCommandLine(executable, arguments),
                    RegistryValueKind.String);
            }
            RemoveLegacyTask();
            DeleteIfExists(script);
        }

        private static string BuildCommandLine(string executable, string arguments)
        {
            return Quote(executable) + (String.IsNullOrWhiteSpace(arguments) ? String.Empty : " " + arguments);
        }

        public static string BuildArguments(string sourceRoot, string runtimeRoot)
        {
            return BuildArguments(sourceRoot, runtimeRoot, true);
        }

        public static string BuildVisibleArguments(string sourceRoot, string runtimeRoot)
        {
            return BuildArguments(sourceRoot, runtimeRoot, false);
        }

        private static string BuildArguments(string sourceRoot, string runtimeRoot, bool minimized)
        {
            var arguments = "--source-root " + Quote(sourceRoot) + " --root " + Quote(runtimeRoot);
            return minimized ? arguments + " --minimized" : arguments;
        }

        private static void RemoveLegacyTask()
        {
            try
            {
                using (var process = new Process())
                {
                    process.StartInfo = new ProcessStartInfo(
                        "schtasks.exe", "/Delete /TN " + Quote(TaskName) + " /F")
                    {
                        UseShellExecute = false,
                        CreateNoWindow = true
                    };
                    process.Start();
                    if (!process.WaitForExit(1000))
                    {
                        try { process.Kill(); } catch { }
                    }
                }
            }
            catch { }
        }

        private static void DeleteIfExists(string path)
        {
            if (File.Exists(path)) File.Delete(path);
        }

        private static string Quote(string value)
        {
            return "\"" + (value ?? String.Empty).Replace("\"", "\\\"") + "\"";
        }
    }
}

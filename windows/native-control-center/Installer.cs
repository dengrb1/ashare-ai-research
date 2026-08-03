using System;
using System.Diagnostics;
using System.Drawing;
using System.IO;
using System.IO.Compression;
using System.Reflection;
using System.Text;
using System.Windows.Forms;
using Microsoft.Win32;

namespace AshareAI.Setup
{
    internal static class Program
    {
        [STAThread]
        private static int Main(string[] args)
        {
            Application.EnableVisualStyles();
            var options = SetupOptions.Parse(args);
            try
            {
                if (!options.Uninstall && !options.Quiet && !SetupLocationDialog.TrySelect(options))
                    return 0;
                if (options.Uninstall) Uninstall(options);
                else Install(options);
                return 0;
            }
            catch (Exception error)
            {
                options.Log("ERROR " + error);
                if (!options.Quiet)
                    MessageBox.Show("安装失败：" + error.Message, "AshareAI 安装程序", MessageBoxButtons.OK, MessageBoxIcon.Error);
                return 1;
            }
        }

        private static void Install(SetupOptions options)
        {
            options.Log("InstallRoot=" + options.InstallRoot);
            options.Log("RuntimeRoot=" + options.RuntimeRoot);
            Directory.CreateDirectory(options.InstallRoot);
            using (var payload = Assembly.GetExecutingAssembly().GetManifestResourceStream("AshareAI.Payload"))
            {
                if (payload == null) throw new InvalidOperationException("安装载荷缺失");
                using (var archive = new ZipArchive(payload, ZipArchiveMode.Read))
                {
                    var root = Path.GetFullPath(options.InstallRoot).TrimEnd(Path.DirectorySeparatorChar) + Path.DirectorySeparatorChar;
                    foreach (var entry in archive.Entries)
                    {
                        var destination = Path.GetFullPath(Path.Combine(root, entry.FullName.Replace('/', Path.DirectorySeparatorChar)));
                        if (!destination.StartsWith(root, StringComparison.OrdinalIgnoreCase)) throw new InvalidDataException("安装载荷包含非法路径");
                        if (String.IsNullOrEmpty(entry.Name)) { Directory.CreateDirectory(destination); continue; }
                        Directory.CreateDirectory(Path.GetDirectoryName(destination));
                        entry.ExtractToFile(destination, true);
                    }
                }
            }
            var setupCopy = Path.Combine(options.InstallRoot, "AshareAI.Setup.exe");
            if (!String.Equals(Path.GetFullPath(Application.ExecutablePath), Path.GetFullPath(setupCopy), StringComparison.OrdinalIgnoreCase))
                File.Copy(Application.ExecutablePath, setupCopy, true);

            var manager = Path.Combine(options.InstallRoot, "AshareAI.NativeControlCenter.exe");
            var cli = Path.Combine(options.InstallRoot, "AshareAI.NativeControlCenter.Cli.exe");
            if (!File.Exists(manager)) throw new FileNotFoundException("管理器缺失", manager);
            if (!File.Exists(cli)) throw new FileNotFoundException("命令行管理器缺失", cli);
            File.WriteAllText(Path.Combine(options.InstallRoot, "runtime-root.txt"), options.RuntimeRoot + Environment.NewLine, Encoding.UTF8);

            if (!options.NoShortcuts)
            {
                CreateShortcut(Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.CommonStartMenu), "Programs", "AshareAI 本机运行管理器.lnk"), manager);
                if (!options.NoDesktopShortcut)
                    CreateShortcut(Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.CommonDesktopDirectory), "AshareAI 本机运行管理器.lnk"), manager);
            }
            using (var key = Registry.LocalMachine.CreateSubKey(@"Software\Microsoft\Windows\CurrentVersion\Uninstall\AshareAI"))
            {
                key.SetValue("DisplayName", "AshareAI 本机运行管理器");
                key.SetValue("DisplayVersion", "2026.08.03.2");
                key.SetValue("Publisher", "AshareAI");
                key.SetValue("DisplayIcon", manager);
                key.SetValue("InstallLocation", options.InstallRoot);
                key.SetValue("UninstallString", "\"" + setupCopy + "\" /uninstall /dir " + Quote(options.InstallRoot));
                key.SetValue("QuietUninstallString", "\"" + setupCopy + "\" /uninstall /quiet /dir " + Quote(options.InstallRoot));
            }

            if (options.InstallDependencies)
            {
                var installCode = RunCli(options, "install");
                if (installCode != 0) throw new InvalidOperationException("依赖安装失败，退出码 " + installCode);
                if (options.StartServices)
                {
                    var startCode = RunCli(options, "start");
                    if (startCode != 0) throw new InvalidOperationException("服务启动失败，退出码 " + startCode);
                }
            }

            if (!options.Quiet)
            {
                MessageBox.Show("管理器已安装。接下来会自动校验、下载并展开运行依赖。", "AshareAI 安装程序", MessageBoxButtons.OK, MessageBoxIcon.Information);
                if (!options.NoStartManager) StartManager(options, manager);
            }
            else
            {
                options.Log("Install completed");
            }
        }

        private static void Uninstall(SetupOptions options)
        {
            var answer = options.Quiet ? DialogResult.Yes : MessageBox.Show("卸载管理器文件？运行数据与数据库会保留。", "卸载 AshareAI", MessageBoxButtons.YesNo, MessageBoxIcon.Question);
            if (answer != DialogResult.Yes) return;
            DeleteIfExists(Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.CommonStartMenu), "Programs", "AshareAI 本机运行管理器.lnk"));
            DeleteIfExists(Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.CommonDesktopDirectory), "AshareAI 本机运行管理器.lnk"));
            Registry.LocalMachine.DeleteSubKeyTree(@"Software\Microsoft\Windows\CurrentVersion\Uninstall\AshareAI", false);
            var command = "$p='" + options.InstallRoot.Replace("'", "''") + "'; Start-Sleep -Seconds 2; if(Test-Path -LiteralPath $p){Remove-Item -Recurse -Force -LiteralPath $p}";
            Process.Start(new ProcessStartInfo("powershell.exe", "-NoLogo -NoProfile -NonInteractive -WindowStyle Hidden -Command \"" + command.Replace("\"", "\\\"") + "\"") { UseShellExecute = false, CreateNoWindow = true });
            options.Log("Uninstall scheduled");
        }

        private static int RunCli(SetupOptions options, string command)
        {
            var cli = Path.Combine(options.InstallRoot, "AshareAI.NativeControlCenter.Cli.exe");
            var args = new StringBuilder();
            args.Append(command);
            args.Append(" --source-root ").Append(Quote(Path.Combine(options.InstallRoot, "app")));
            args.Append(" --root ").Append(Quote(options.RuntimeRoot));
            if (options.StartServices && command == "start") args.Append(" --research-mode SERIAL");
            options.Log("Running CLI: " + command);
            var process = new Process();
            process.StartInfo = new ProcessStartInfo(cli, args.ToString())
            {
                UseShellExecute = false,
                CreateNoWindow = true,
                RedirectStandardOutput = true,
                RedirectStandardError = true
            };
            process.Start();
            var stdout = process.StandardOutput.ReadToEnd();
            var stderr = process.StandardError.ReadToEnd();
            process.WaitForExit();
            if (!String.IsNullOrWhiteSpace(stdout)) options.Log(stdout.TrimEnd());
            if (!String.IsNullOrWhiteSpace(stderr)) options.Log(stderr.TrimEnd());
            return process.ExitCode;
        }

        private static void StartManager(SetupOptions options, string manager)
        {
            var arguments = "--source-root " + Quote(Path.Combine(options.InstallRoot, "app")) + " --root " + Quote(options.RuntimeRoot) + " --auto-install";
            Process.Start(new ProcessStartInfo(manager, arguments) { UseShellExecute = true });
        }

        private static void CreateShortcut(string path, string target)
        {
            Directory.CreateDirectory(Path.GetDirectoryName(path));
            var shell = Activator.CreateInstance(Type.GetTypeFromProgID("WScript.Shell"));
            var shortcut = shell.GetType().InvokeMember("CreateShortcut", BindingFlags.InvokeMethod, null, shell, new object[] { path });
            shortcut.GetType().InvokeMember("TargetPath", BindingFlags.SetProperty, null, shortcut, new object[] { target });
            shortcut.GetType().InvokeMember("WorkingDirectory", BindingFlags.SetProperty, null, shortcut, new object[] { Path.GetDirectoryName(target) });
            shortcut.GetType().InvokeMember("Save", BindingFlags.InvokeMethod, null, shortcut, null);
        }

        private static void DeleteIfExists(string path) { if (File.Exists(path)) File.Delete(path); }
        private static string Quote(string value) { return "\"" + value.Replace("\"", "\\\"") + "\""; }
    }

    internal sealed class SetupOptions
    {
        public string InstallRoot = DefaultInstallRoot();
        public string RuntimeRoot;
        public bool Uninstall;
        public bool Quiet;
        public bool NoShortcuts;
        public bool NoDesktopShortcut;
        public bool NoStartManager;
        public bool InstallDependencies;
        public bool StartServices;
        public string LogPath = Path.Combine(Path.GetTempPath(), "AshareAI-Setup.log");

        public static SetupOptions Parse(string[] args)
        {
            var options = new SetupOptions();
            for (var index = 0; index < args.Length; index++)
            {
                var value = args[index];
                var lower = value.ToLowerInvariant();
                if (lower == "/uninstall" || lower == "--uninstall") { options.Uninstall = true; continue; }
                if (lower == "/quiet" || lower == "/silent" || lower == "/verysilent" || lower == "/qn" || lower == "--quiet" || lower == "--silent") { options.Quiet = true; continue; }
                if (lower == "/no-shortcuts" || lower == "--no-shortcuts") { options.NoShortcuts = true; continue; }
                if (lower == "/no-desktop-shortcut" || lower == "--no-desktop-shortcut") { options.NoDesktopShortcut = true; continue; }
                if (lower == "/no-start" || lower == "--no-start") { options.NoStartManager = true; continue; }
                if (lower == "/install-deps" || lower == "--install-deps") { options.InstallDependencies = true; continue; }
                if (lower == "/no-install-deps" || lower == "--no-install-deps") { options.InstallDependencies = false; continue; }
                if (lower == "/start-services" || lower == "--start-services") { options.StartServices = true; options.InstallDependencies = true; continue; }
                string installRoot;
                if (TryReadValue(args, ref index, value, lower, "/dir", "--install-dir", out installRoot)) { options.InstallRoot = Path.GetFullPath(installRoot); continue; }
                string runtimeRoot;
                if (TryReadValue(args, ref index, value, lower, "/root", "--runtime-root", out runtimeRoot)) { options.RuntimeRoot = Path.GetFullPath(runtimeRoot); continue; }
                string logPath;
                if (TryReadValue(args, ref index, value, lower, "/log", "--log", out logPath)) { options.LogPath = Path.GetFullPath(logPath); continue; }
                throw new ArgumentException("未知安装参数：" + value);
            }
            if (String.IsNullOrWhiteSpace(options.RuntimeRoot))
                options.RuntimeRoot = Path.GetFullPath(Path.Combine(options.InstallRoot, "runtime"));
            if (options.Quiet && !HasSwitch(args, "/no-install-deps", "--no-install-deps"))
                options.InstallDependencies = true;
            return options;
        }

        private static string DefaultInstallRoot()
        {
            var managerDirectory = Path.GetDirectoryName(Application.ExecutablePath);
            return Path.Combine(managerDirectory, "AshareAI");
        }

        public void Log(string message)
        {
            try
            {
                Directory.CreateDirectory(Path.GetDirectoryName(LogPath));
                File.AppendAllText(LogPath, DateTime.UtcNow.ToString("o") + " " + message + Environment.NewLine, Encoding.UTF8);
            }
            catch { }
        }

        private static bool TryReadValue(string[] args, ref int index, string raw, string lower, string slashName, string longName, out string result)
        {
            result = null;
            if (lower.StartsWith(slashName + "=", StringComparison.OrdinalIgnoreCase) || lower.StartsWith(longName + "=", StringComparison.OrdinalIgnoreCase))
            {
                result = raw.Substring(raw.IndexOf('=') + 1).Trim('"');
                return true;
            }
            if ((lower == slashName || lower == longName) && index + 1 < args.Length)
            {
                result = args[++index];
                return true;
            }
            return false;
        }

        private static bool HasSwitch(string[] args, params string[] names)
        {
            foreach (var arg in args)
                foreach (var name in names)
                    if (String.Equals(arg, name, StringComparison.OrdinalIgnoreCase)) return true;
            return false;
        }
    }

    internal static class SetupLocationDialog
    {
        public static bool TrySelect(SetupOptions options)
        {
            using (var form = new Form())
            {
                form.Text = "AshareAI 安装位置";
                form.StartPosition = FormStartPosition.CenterScreen;
                form.FormBorderStyle = FormBorderStyle.FixedDialog;
                form.MinimizeBox = false;
                form.MaximizeBox = false;
                form.ClientSize = new Size(660, 215);

                var title = new Label { Text = "选择 AshareAI 的安装位置", Location = new Point(18, 16), Size = new Size(600, 24), Font = new Font("Segoe UI Semibold", 12F) };
                var hint = new Label { Text = "管理器文件和运行依赖可以放在不同目录；运行目录默认位于管理器目录下的 runtime 文件夹。", Location = new Point(18, 47), Size = new Size(620, 36), ForeColor = Color.FromArgb(75, 85, 99) };
                var installLabel = new Label { Text = "管理器目录", Location = new Point(18, 92), Size = new Size(92, 24) };
                var install = new TextBox { Text = options.InstallRoot, Location = new Point(112, 89), Size = new Size(450, 25) };
                var installBrowse = new Button { Text = "浏览", Location = new Point(570, 88), Size = new Size(72, 28) };
                var runtimeLabel = new Label { Text = "运行目录", Location = new Point(18, 130), Size = new Size(92, 24) };
                var runtime = new TextBox { Text = options.RuntimeRoot, Location = new Point(112, 127), Size = new Size(450, 25) };
                var runtimeBrowse = new Button { Text = "浏览", Location = new Point(570, 126), Size = new Size(72, 28) };
                var cancel = new Button { Text = "取消", DialogResult = DialogResult.Cancel, Location = new Point(460, 174), Size = new Size(82, 28) };
                var accept = new Button { Text = "继续安装", DialogResult = DialogResult.OK, Location = new Point(550, 174), Size = new Size(92, 28) };
                var runtimeFollowsInstall = String.Equals(
                    runtime.Text.Trim().TrimEnd('\\'),
                    Path.Combine(install.Text.Trim(), "runtime").TrimEnd('\\'),
                    StringComparison.OrdinalIgnoreCase);
                var updatingRuntime = false;

                installBrowse.Click += delegate { BrowseForFolder(form, install); };
                runtimeBrowse.Click += delegate { BrowseForFolder(form, runtime); };
                install.TextChanged += delegate
                {
                    if (!runtimeFollowsInstall) return;
                    updatingRuntime = true;
                    runtime.Text = Path.Combine(install.Text.Trim(), "runtime");
                    updatingRuntime = false;
                };
                runtime.TextChanged += delegate { if (!updatingRuntime) runtimeFollowsInstall = false; };
                accept.Click += delegate
                {
                    try
                    {
                        var installRoot = Path.GetFullPath(install.Text.Trim());
                        var runtimeRoot = Path.GetFullPath(runtime.Text.Trim());
                        if (String.IsNullOrWhiteSpace(install.Text) || String.IsNullOrWhiteSpace(runtime.Text))
                            throw new ArgumentException("安装目录和运行目录都不能为空。");
                        if (String.Equals(installRoot.TrimEnd('\\'), runtimeRoot.TrimEnd('\\'), StringComparison.OrdinalIgnoreCase))
                            throw new ArgumentException("运行目录不能与管理器目录相同，请选择一个子目录或其他目录。");
                        options.InstallRoot = installRoot;
                        options.RuntimeRoot = runtimeRoot;
                    }
                    catch (Exception error)
                    {
                        MessageBox.Show(form, error.Message, "安装位置无效", MessageBoxButtons.OK, MessageBoxIcon.Warning);
                        form.DialogResult = DialogResult.None;
                    }
                };

                form.Controls.Add(title); form.Controls.Add(hint); form.Controls.Add(installLabel); form.Controls.Add(install); form.Controls.Add(installBrowse);
                form.Controls.Add(runtimeLabel); form.Controls.Add(runtime); form.Controls.Add(runtimeBrowse); form.Controls.Add(cancel); form.Controls.Add(accept);
                form.AcceptButton = accept; form.CancelButton = cancel;
                return form.ShowDialog() == DialogResult.OK;
            }
        }

        private static void BrowseForFolder(Form owner, TextBox target)
        {
            var selected = target.Text.Trim();
            while (!String.IsNullOrWhiteSpace(selected) && !Directory.Exists(selected))
            {
                var parent = Path.GetDirectoryName(selected);
                if (String.IsNullOrEmpty(parent) || String.Equals(parent, selected, StringComparison.OrdinalIgnoreCase)) break;
                selected = parent;
            }
            using (var dialog = new FolderBrowserDialog { Description = "选择目录", SelectedPath = selected })
                if (dialog.ShowDialog(owner) == DialogResult.OK) target.Text = dialog.SelectedPath;
        }
    }
}

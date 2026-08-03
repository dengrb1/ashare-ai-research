using System;
using System.Collections;
using System.Collections.Generic;
using System.Diagnostics;
using System.Drawing;
using System.IO;
using System.Linq;
using System.Reflection;
using System.Text;
using System.Web.Script.Serialization;
using System.Windows.Forms;

namespace AshareAI.NativeControlCenter
{
    internal static class Program
    {
        [STAThread]
        private static int Main(string[] args)
        {
            try
            {
                if (CommandLine.IsCliRequest(args))
                    return ConsoleCommand.Run(args, false);
                Application.EnableVisualStyles();
                Application.SetCompatibleTextRenderingDefault(false);
                var options = Options.Parse(args);
                Application.Run(new MainForm(options));
                return 0;
            }
            catch (Exception error)
            {
                MessageBox.Show(error.Message, "AshareAI 本机运行管理器", MessageBoxButtons.OK, MessageBoxIcon.Error);
                return 1;
            }
        }
    }

    internal sealed class Options
    {
        public string SourceRoot;
        public string RuntimeRoot;
        public bool AutoInstall;

        public static Options Parse(string[] args)
        {
            var options = new Options();
            for (var index = 0; index < args.Length; index++)
            {
                var value = args[index].ToLowerInvariant();
                if ((value == "--source-root" || value == "-s") && index + 1 < args.Length)
                    options.SourceRoot = Path.GetFullPath(args[++index]);
                else if ((value == "--root" || value == "-r") && index + 1 < args.Length)
                    options.RuntimeRoot = Path.GetFullPath(args[++index]);
                else if (value == "--auto-install")
                    options.AutoInstall = true;
            }
            if (String.IsNullOrEmpty(options.SourceRoot))
                options.SourceRoot = FindSourceRoot(AppDomain.CurrentDomain.BaseDirectory);
            if (String.IsNullOrEmpty(options.RuntimeRoot))
                options.RuntimeRoot = Environment.GetEnvironmentVariable("ASHARE_NATIVE_ROOT");
            if (String.IsNullOrEmpty(options.RuntimeRoot))
                options.RuntimeRoot = ReadConfiguredRuntimeRoot();
            if (String.IsNullOrEmpty(options.RuntimeRoot))
                options.RuntimeRoot = DefaultRuntimeRoot(options.SourceRoot);
            options.RuntimeRoot = Path.GetFullPath(options.RuntimeRoot);
            if (String.IsNullOrEmpty(options.SourceRoot))
                throw new InvalidOperationException("未找到 AshareAI 应用载荷，请重新安装管理器。");
            return options;
        }

        private static string ReadConfiguredRuntimeRoot()
        {
            var configuration = Path.Combine(AppDomain.CurrentDomain.BaseDirectory, "runtime-root.txt");
            try
            {
                if (File.Exists(configuration))
                {
                    var value = File.ReadAllText(configuration).Trim();
                    if (!String.IsNullOrEmpty(value)) return value;
                }
            }
            catch { }
            return null;
        }

        private static string DefaultRuntimeRoot(string sourceRoot)
        {
            var managerDirectory = Path.GetFullPath(AppDomain.CurrentDomain.BaseDirectory);
            var candidate = Path.Combine(managerDirectory, "runtime");
            if (!String.IsNullOrEmpty(sourceRoot) && IsInside(candidate, sourceRoot))
                return Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), "AshareAI", "runtime");
            return candidate;
        }

        private static bool IsInside(string path, string parent)
        {
            var normalizedPath = Path.GetFullPath(path).TrimEnd(Path.DirectorySeparatorChar) + Path.DirectorySeparatorChar;
            var normalizedParent = Path.GetFullPath(parent).TrimEnd(Path.DirectorySeparatorChar) + Path.DirectorySeparatorChar;
            return normalizedPath.StartsWith(normalizedParent, StringComparison.OrdinalIgnoreCase);
        }

        private static string FindSourceRoot(string start)
        {
            var current = new DirectoryInfo(start);
            while (current != null)
            {
                var packagedApp = Path.Combine(current.FullName, "app");
                if (File.Exists(Path.Combine(packagedApp, "pyproject.toml")) && Directory.Exists(Path.Combine(packagedApp, "src")))
                    return packagedApp;
                if (File.Exists(Path.Combine(current.FullName, "scripts", "native", "ashare-native.ps1")))
                    return current.FullName;
                current = current.Parent;
            }
            return null;
        }
    }

    internal static class EmbeddedAssets
    {
        public static string ExtractController(string runtimeRoot)
        {
            var directory = Path.Combine(Path.GetFullPath(runtimeRoot), "controller");
            Directory.CreateDirectory(directory);
            WriteResource("AshareAI.Controller", Path.Combine(directory, "ashare-native.ps1"));
            WriteResource("AshareAI.DependencyLock", Path.Combine(directory, "dependencies.lock.json"));
            return Path.Combine(directory, "ashare-native.ps1");
        }

        private static void WriteResource(string name, string destination)
        {
            using (var source = Assembly.GetExecutingAssembly().GetManifestResourceStream(name))
            {
                if (source == null) throw new InvalidOperationException("安装包内置资源缺失：" + name);
                using (var memory = new MemoryStream())
                {
                    source.CopyTo(memory);
                    var bytes = memory.ToArray();
                    if (File.Exists(destination) && File.ReadAllBytes(destination).SequenceEqual(bytes)) return;
                    var temporary = destination + ".tmp";
                    File.WriteAllBytes(temporary, bytes);
                    if (File.Exists(destination)) File.Delete(destination);
                    File.Move(temporary, destination);
                }
            }
        }
    }

    internal sealed class MainForm : Form
    {
        private readonly Options options;
        private readonly string controller;
        private readonly JavaScriptSerializer json = new JavaScriptSerializer();
        private readonly TextBox activity = new TextBox();
        private readonly TextBox watchdogLog = new TextBox();
        private readonly DataGridView services = new DataGridView();
        private readonly Label state = new Label();
        private readonly Label health = new Label();
        private readonly Label memory = new Label();
        private readonly Label watchdog = new Label();
        private readonly Label ports = new Label();
        private readonly Label footer = new Label();
        private readonly TextBox root = new TextBox();
        private readonly ComboBox mode = new ComboBox();
        private readonly NumericUpDown workers = new NumericUpDown();
        private readonly NumericUpDown watchdogInterval = new NumericUpDown();
        private readonly CheckBox autoRefresh = new CheckBox();
        private readonly Button openWeb = new Button();
        private readonly Timer poll = new Timer();
        private readonly Timer refresh = new Timer();
        private readonly List<Button> actionButtons = new List<Button>();
        private Process activeProcess;
        private string activeOperation;
        private readonly StringBuilder activeOutput = new StringBuilder();
        private readonly StringBuilder activeError = new StringBuilder();
        private bool refreshPending;
        private string queuedOperation;
        private bool queuedAsJson;
        private Dictionary<string, object> lastReport;

        public MainForm(Options options)
        {
            this.options = options;
            controller = EmbeddedAssets.ExtractController(options.RuntimeRoot);
            Text = "AshareAI 本机运行管理器";
            StartPosition = FormStartPosition.CenterScreen;
            MinimumSize = new Size(980, 680);
            Size = new Size(1160, 780);
            BackColor = Color.FromArgb(246, 247, 249);
            Font = new Font("Segoe UI", 9F);
            BuildLayout();
            LoadSettings();
            workers.Enabled = String.Equals(Convert.ToString(mode.SelectedItem), "DUAL", StringComparison.Ordinal);
            poll.Interval = 500;
            poll.Tick += PollTick;
            refresh.Interval = 10000;
            refresh.Tick += delegate { if (autoRefresh.Checked && activeProcess == null) StartCommand("status", true); };
            poll.Start();
            refresh.Start();
            FormClosing += HandleClosing;
            Shown += delegate
            {
                if (options.AutoInstall && !File.Exists(Path.Combine(options.RuntimeRoot, ".env"))) StartCommand("install", false);
                else StartCommand("status", true);
            };
        }

        private static Label Label(string text, int x, int y, int width, int height)
        {
            return new Label { Text = text, Location = new Point(x, y), Size = new Size(width, height), AutoEllipsis = true };
        }

        private static Panel Card(int x, int y, int width, int height)
        {
            return new Panel
            {
                Location = new Point(x, y),
                Size = new Size(width, height),
                BackColor = Color.White,
                BorderStyle = BorderStyle.FixedSingle
            };
        }

        private static void StyleButton(Button button, bool primary)
        {
            button.FlatStyle = FlatStyle.Flat;
            button.FlatAppearance.BorderSize = 0;
            button.BackColor = primary ? Color.FromArgb(27, 111, 91) : Color.FromArgb(232, 237, 242);
            button.ForeColor = primary ? Color.White : Color.FromArgb(31, 41, 55);
            button.Font = new Font("Segoe UI Semibold", 9F);
        }

        private void BuildLayout()
        {
            MinimumSize = new Size(940, 640);
            Size = new Size(1040, 720);
            BackColor = Color.FromArgb(242, 245, 248);

            var header = new Panel { Dock = DockStyle.Top, Height = 74, BackColor = Color.White };
            var accent = new Panel { Dock = DockStyle.Left, Width = 5, BackColor = Color.FromArgb(27, 111, 91) };
            var title = Label("AshareAI 本机运行管理器", 24, 12, 520, 28);
            title.ForeColor = Color.FromArgb(17, 24, 39); title.Font = new Font("Segoe UI Semibold", 16F);
            var subtitle = Label("安装、启动、诊断和查看本机运行状态", 26, 43, 650, 20);
            subtitle.ForeColor = Color.FromArgb(107, 114, 128);
            header.Controls.Add(accent); header.Controls.Add(title); header.Controls.Add(subtitle); Controls.Add(header);

            var settings = Card(14, 90, 996, 92);
            settings.Anchor = AnchorStyles.Top | AnchorStyles.Left | AnchorStyles.Right;
            settings.Controls.Add(Label("运行目录", 18, 17, 82, 22));
            root.Location = new Point(100, 14); root.Size = new Size(680, 25); root.Anchor = AnchorStyles.Top | AnchorStyles.Left | AnchorStyles.Right; root.Text = options.RuntimeRoot;
            settings.Controls.Add(root);
            var browse = new Button { Text = "浏览", Location = new Point(792, 13), Size = new Size(76, 28), Anchor = AnchorStyles.Top | AnchorStyles.Right };
            StyleButton(browse, false); browse.Click += Browse; settings.Controls.Add(browse);
            var openRoot = new Button { Text = "目录", Location = new Point(874, 13), Size = new Size(62, 28), Anchor = AnchorStyles.Top | AnchorStyles.Right };
            StyleButton(openRoot, false); openRoot.Click += delegate { Directory.CreateDirectory(root.Text.Trim()); Process.Start("explorer.exe", Quote(root.Text.Trim())); }; settings.Controls.Add(openRoot);
            var save = new Button { Text = "保存", Location = new Point(942, 13), Size = new Size(42, 28), Anchor = AnchorStyles.Top | AnchorStyles.Right };
            StyleButton(save, true); save.Click += delegate { SaveSettings(); }; settings.Controls.Add(save);
            settings.Controls.Add(Label("模式", 18, 55, 54, 24));
            mode.DropDownStyle = ComboBoxStyle.DropDownList; mode.Location = new Point(100, 52); mode.Size = new Size(112, 25); mode.Items.AddRange(new object[] { "SERIAL", "DUAL" }); mode.SelectedItem = "SERIAL"; settings.Controls.Add(mode);
            settings.Controls.Add(Label("研究进程", 232, 55, 70, 24));
            workers.Location = new Point(304, 52); workers.Size = new Size(58, 25); workers.Minimum = 0; workers.Maximum = 2; settings.Controls.Add(workers);
            settings.Controls.Add(Label("看门狗秒数", 388, 55, 84, 24));
            watchdogInterval.Location = new Point(478, 52); watchdogInterval.Size = new Size(66, 25); watchdogInterval.Minimum = 5; watchdogInterval.Maximum = 300; watchdogInterval.Value = 10; settings.Controls.Add(watchdogInterval);
            autoRefresh.Text = "自动刷新"; autoRefresh.Location = new Point(576, 53); autoRefresh.Size = new Size(110, 24); autoRefresh.Checked = true; settings.Controls.Add(autoRefresh);
            Controls.Add(settings);

            var actions = Card(14, 194, 996, 48);
            actions.Anchor = AnchorStyles.Top | AnchorStyles.Left | AnchorStyles.Right;
            AddAction(actions, "启动", "start", 12, 74); AddAction(actions, "停止", "stop", 92, 74); AddAction(actions, "重启", "restart", 172, 74); AddAction(actions, "修复", "repair", 252, 74); AddAction(actions, "安装更新", "install", 332, 96); AddAction(actions, "诊断", "doctor", 434, 74);
            var refreshButton = new Button { Text = "刷新", Location = new Point(902, 9), Size = new Size(76, 30), Anchor = AnchorStyles.Top | AnchorStyles.Right }; StyleButton(refreshButton, false); refreshButton.Click += delegate { StartCommand("status", true); }; actions.Controls.Add(refreshButton);
            openWeb.Text = "打开 Web"; openWeb.Location = new Point(806, 9); openWeb.Size = new Size(86, 30); openWeb.Anchor = AnchorStyles.Top | AnchorStyles.Right; openWeb.Enabled = false; StyleButton(openWeb, true); openWeb.Click += delegate { if (lastReport != null) Process.Start(String.Format("http://127.0.0.1:{0}/", Number(lastReport, "ports", "api"))); }; actions.Controls.Add(openWeb); Controls.Add(actions);

            var summary = Card(14, 254, 996, 76);
            summary.Anchor = AnchorStyles.Top | AnchorStyles.Left | AnchorStyles.Right;
            state.Text = "正在检查..."; state.Location = new Point(18, 24); state.Size = new Size(170, 30); state.Font = new Font("Segoe UI Semibold", 15F); summary.Controls.Add(state);
            health.Text = "健康状态：--"; health.Location = new Point(220, 26); health.Size = new Size(170, 24); summary.Controls.Add(health);
            memory.Text = "内存：--"; memory.Location = new Point(408, 26); memory.Size = new Size(150, 24); summary.Controls.Add(memory);
            watchdog.Text = "看门狗：--"; watchdog.Location = new Point(574, 26); watchdog.Size = new Size(170, 24); summary.Controls.Add(watchdog);
            ports.Text = "端口：--"; ports.Location = new Point(756, 16); ports.Size = new Size(220, 46); summary.Controls.Add(ports); Controls.Add(summary);

            var tabs = new TabControl { Location = new Point(14, 342), Size = new Size(996, 320), Anchor = AnchorStyles.Top | AnchorStyles.Bottom | AnchorStyles.Left | AnchorStyles.Right };
            var servicesPage = new TabPage("服务");
            services.Dock = DockStyle.Fill; services.ReadOnly = true; services.AllowUserToAddRows = false; services.AllowUserToDeleteRows = false; services.AutoSizeColumnsMode = DataGridViewAutoSizeColumnsMode.Fill; services.BackgroundColor = Color.White; services.BorderStyle = BorderStyle.None; services.RowHeadersVisible = false;
            foreach (var column in new[] { "服务", "角色", "PID", "健康", "内存 MiB", "内嵌于" }) services.Columns.Add(column.Replace(" ", ""), column);
            servicesPage.Controls.Add(services); tabs.TabPages.Add(servicesPage);
            var activityPage = new TabPage("活动记录"); ConfigureTextBox(activity); activityPage.Controls.Add(activity); tabs.TabPages.Add(activityPage);
            var logsPage = new TabPage("看门狗日志"); ConfigureTextBox(watchdogLog); logsPage.Controls.Add(watchdogLog); tabs.TabPages.Add(logsPage);
            tabs.SelectedIndexChanged += delegate { if (tabs.SelectedTab == logsPage) LoadWatchdogLog(); }; Controls.Add(tabs);
            footer.Text = "就绪"; footer.Location = new Point(16, 670); footer.Size = new Size(980, 24); footer.Anchor = AnchorStyles.Bottom | AnchorStyles.Left | AnchorStyles.Right; footer.ForeColor = Color.FromArgb(75, 85, 99); Controls.Add(footer);
            mode.SelectedIndexChanged += delegate { workers.Enabled = Convert.ToString(mode.SelectedItem) == "DUAL"; if (!workers.Enabled) workers.Value = 0; };
        }

        private void ConfigureTextBox(TextBox box) { box.Dock = DockStyle.Fill; box.Multiline = true; box.ReadOnly = true; box.ScrollBars = ScrollBars.Both; box.WordWrap = false; box.Font = new Font("Consolas", 9F); }
        private void AddAction(Panel panel, string text, string operation, int x, int width) { var button = new Button { Text = text, Tag = operation, Location = new Point(x, 9), Size = new Size(width, 30) }; StyleButton(button, operation == "start"); button.Click += Action; actionButtons.Add(button); panel.Controls.Add(button); }
        private void Action(object sender, EventArgs args) { var button = (Button)sender; var operation = Convert.ToString(button.Tag); if (operation == "install" && MessageBox.Show(this, "要在所选目录安装或更新全部本机依赖吗？此过程可能需要数分钟。", "确认安装", MessageBoxButtons.YesNo, MessageBoxIcon.Question) != DialogResult.Yes) return; SaveSettings(); StartCommand(operation, operation == "doctor"); }
        private void Browse(object sender, EventArgs args) { using (var dialog = new FolderBrowserDialog { Description = "选择 AshareAI 本机运行目录", SelectedPath = root.Text }) if (dialog.ShowDialog(this) == DialogResult.OK) { root.Text = dialog.SelectedPath; SaveRuntimeRootSelection(); LoadSettings(); StartCommand("status", true); } }

        private string SettingsPath { get { return Path.Combine(Path.GetFullPath(root.Text.Trim()), "config", "native-gui.json"); } }
        private string RuntimeRootConfigPath { get { return Path.Combine(AppDomain.CurrentDomain.BaseDirectory, "runtime-root.txt"); } }
        private void SaveRuntimeRootSelection() { try { File.WriteAllText(RuntimeRootConfigPath, Path.GetFullPath(root.Text.Trim()) + Environment.NewLine, Encoding.UTF8); } catch (Exception error) { AddActivity("保存运行目录失败：" + error.Message); } }
        private void SaveSettings() { SaveRuntimeRootSelection(); Directory.CreateDirectory(Path.GetDirectoryName(SettingsPath)); var data = new Dictionary<string, object> { { "research_mode", Convert.ToString(mode.SelectedItem) }, { "research_workers", (int)workers.Value }, { "watchdog_interval_seconds", (int)watchdogInterval.Value }, { "auto_refresh", autoRefresh.Checked } }; File.WriteAllText(SettingsPath, json.Serialize(data), Encoding.UTF8); AddActivity("设置已保存到 " + SettingsPath); }
        private void LoadSettings() { if (!File.Exists(SettingsPath)) return; try { var data = json.DeserializeObject(File.ReadAllText(SettingsPath)) as Dictionary<string, object>; if (data == null) return; if (data.ContainsKey("research_mode")) mode.SelectedItem = Convert.ToString(data["research_mode"]); if (data.ContainsKey("research_workers")) workers.Value = Math.Min(2, Math.Max(0, Convert.ToDecimal(data["research_workers"]))); if (data.ContainsKey("watchdog_interval_seconds")) watchdogInterval.Value = Math.Min(300, Math.Max(5, Convert.ToDecimal(data["watchdog_interval_seconds"]))); if (data.ContainsKey("auto_refresh")) autoRefresh.Checked = Convert.ToBoolean(data["auto_refresh"]); } catch (Exception error) { AddActivity("读取管理器设置失败：" + error.Message); } }
        private void AddActivity(string message) { activity.AppendText("[" + DateTime.Now.ToString("HH:mm:ss") + "] " + message + Environment.NewLine); }
        private static string Quote(string value) { return "\"" + value.Replace("\"", "\\\"") + "\""; }
        private static bool HasNativeInstallation(string runtime)
        {
            return File.Exists(Path.Combine(runtime, ".env")) &&
                File.Exists(Path.Combine(runtime, "config", "native-paths.json")) &&
                File.Exists(Path.Combine(runtime, "config", "native-ports.json")) &&
                File.Exists(Path.Combine(runtime, "venv", "Scripts", "python.exe")) &&
                File.Exists(Path.Combine(runtime, "web", "index.html"));
        }

        private void StartCommand(string operation, bool asJson)
        {
            var runtime = Path.GetFullPath(root.Text.Trim());
            if ((operation == "start" || operation == "restart" || operation == "repair") && !HasNativeInstallation(runtime))
            {
                AddActivity("运行目录尚未安装本机依赖：请先执行“安装更新”。");
                footer.Text = "尚未安装，请先安装更新";
                MessageBox.Show(this, "当前运行目录还没有安装本机依赖。请先点击“安装更新”，完成后再启动服务。", "尚未安装", MessageBoxButtons.OK, MessageBoxIcon.Information);
                return;
            }
            if (activeProcess != null && !activeProcess.HasExited)
            {
                if (operation == "status") { refreshPending = true; return; }
                if (activeOperation == "status")
                {
                    if (String.IsNullOrEmpty(queuedOperation))
                    {
                        queuedOperation = operation;
                        queuedAsJson = asJson;
                        AddActivity("状态刷新完成后将执行：" + OperationName(operation));
                    }
                    return;
                }
                return;
            }
            Directory.CreateDirectory(Path.Combine(runtime, "logs"));
            var arguments = "-NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass -File " + Quote(controller) + " -Command " + operation + " -Root " + Quote(runtime) + " -SourceRoot " + Quote(options.SourceRoot);
            if (operation == "start" || operation == "restart" || operation == "install") arguments += " -ResearchMode " + Convert.ToString(mode.SelectedItem) + " -ResearchWorkers " + workers.Value + " -WatchdogIntervalSeconds " + watchdogInterval.Value;
            if (asJson) arguments += " -Json";
            if (operation == "status") arguments += " -Fast";
            activeOperation = operation; activeOutput.Clear(); activeError.Clear(); refreshPending = false; SetBusy(true, "正在执行“" + OperationName(operation) + "”…", operation != "status"); AddActivity("正在运行：" + OperationName(operation));
            activeProcess = new Process(); activeProcess.StartInfo = new ProcessStartInfo("powershell.exe", arguments) { UseShellExecute = false, CreateNoWindow = true, RedirectStandardOutput = true, RedirectStandardError = true };
            activeProcess.OutputDataReceived += delegate(object sender, DataReceivedEventArgs e) { if (e.Data != null) lock (activeOutput) activeOutput.AppendLine(e.Data); };
            activeProcess.ErrorDataReceived += delegate(object sender, DataReceivedEventArgs e) { if (e.Data != null) lock (activeError) activeError.AppendLine(e.Data); };
            activeProcess.EnableRaisingEvents = true; activeProcess.Exited += delegate { BeginInvoke((Action)CompleteCommand); }; activeProcess.Start(); activeProcess.BeginOutputReadLine(); activeProcess.BeginErrorReadLine();
        }

        private void CompleteCommand()
        {
            if (activeProcess == null) return;
            activeProcess.WaitForExit();
            var operation = activeOperation; var code = activeProcess.ExitCode; string output; string error; lock (activeOutput) output = activeOutput.ToString(); lock (activeError) error = activeError.ToString(); activeProcess.Dispose(); activeProcess = null; SetBusy(false, "就绪", operation != "status");
            if (operation == "status")
            {
                try { var report = json.DeserializeObject(output) as Dictionary<string, object>; if (report == null) throw new InvalidOperationException("状态响应为空"); UpdateReport(report); footer.Text = "状态更新时间：" + DateTime.Now.ToString("HH:mm:ss"); }
                catch { AddActivity("状态命令未返回有效 JSON（退出码 " + code + ")."); if (!String.IsNullOrWhiteSpace(error)) AddActivity(error.Trim()); }
                if (!String.IsNullOrEmpty(queuedOperation))
                {
                    var next = queuedOperation; var nextAsJson = queuedAsJson; queuedOperation = null; queuedAsJson = false;
                    StartCommand(next, nextAsJson);
                }
                return;
            }
            var combined = (output.Trim() + Environment.NewLine + error.Trim()).Trim(); if (combined.Length > 0) AddActivity(combined); AddActivity(OperationName(operation) + "完成，退出码 " + code);
            if (code != 0) MessageBox.Show(this, combined.Length == 0 ? OperationName(operation) + "失败，退出码 " + code : combined, "AshareAI 命令失败", MessageBoxButtons.OK, MessageBoxIcon.Error);
            refreshPending = true; if (refreshPending) { refreshPending = false; StartCommand("status", true); }
        }

        private void UpdateReport(Dictionary<string, object> report)
        {
            lastReport = report; var desired = TextValue(report, "desired_state", "STOPPED"); var healthy = BoolValue(report, "runtime_healthy"); state.Text = StatusName(desired); state.ForeColor = healthy ? Color.FromArgb(19, 128, 74) : Color.FromArgb(190, 76, 20); health.Text = "健康状态：" + (healthy ? "健康" : "未就绪"); memory.Text = "内存：" + TextValue(report, "total_working_set_mib", "0") + " MiB";
            var watch = report.ContainsKey("watchdog") && report["watchdog"] is Dictionary<string, object> ? TextValue((Dictionary<string, object>)report["watchdog"], "status", "MISSING") : "MISSING"; watchdog.Text = "看门狗：" + StatusName(watch);
            var p = report.ContainsKey("ports") ? report["ports"] as Dictionary<string, object> : null; ports.Text = p == null ? "端口：--" : String.Format("端口：PG {0}  Redis {1}\r\nAPI {2}  搜索 {3}", TextValue(p, "postgres", "--"), TextValue(p, "redis", "--"), TextValue(p, "api", "--"), TextValue(p, "searxng", "--")); openWeb.Enabled = healthy && p != null;
            services.Rows.Clear(); var rows = report.ContainsKey("services") ? report["services"] as IEnumerable : null; if (rows != null) foreach (var item in rows) { var row = item as Dictionary<string, object>; if (row != null) services.Rows.Add(TextValue(row, "service", ""), TextValue(row, "role", ""), TextValue(row, "pid", ""), BoolValue(row, "healthy") ? "是" : "否", TextValue(row, "working_set_mib", "0"), TextValue(row, "embedded_in", "")); }
        }

        private void LoadWatchdogLog() { var path = Path.Combine(Path.GetFullPath(root.Text.Trim()), "logs", "watchdog.log"); if (!File.Exists(path)) { watchdogLog.Text = "暂无看门狗日志。"; return; } var lines = File.ReadAllLines(path); watchdogLog.Text = String.Join(Environment.NewLine, lines.Skip(Math.Max(0, lines.Length - 500))); watchdogLog.SelectionStart = watchdogLog.TextLength; watchdogLog.ScrollToCaret(); }
        private void SetBusy(bool busy, string message, bool lockActions) { if (lockActions) foreach (var button in actionButtons) button.Enabled = !busy; footer.Text = message; UseWaitCursor = false; Cursor = Cursors.Default; }
        private void PollTick(object sender, EventArgs args) { if (activeProcess == null || !activeProcess.HasExited) return; CompleteCommand(); }
        private void HandleClosing(object sender, FormClosingEventArgs args) { if (activeProcess != null && !activeProcess.HasExited && MessageBox.Show(this, "管理命令仍在运行。要直接关闭窗口并让命令继续吗？", "命令正在运行", MessageBoxButtons.YesNo, MessageBoxIcon.Warning) != DialogResult.Yes) args.Cancel = true; }
        private static string TextValue(Dictionary<string, object> data, string key, string fallback) { return data != null && data.ContainsKey(key) && data[key] != null ? Convert.ToString(data[key]) : fallback; }
        private static bool BoolValue(Dictionary<string, object> data, string key) { return data != null && data.ContainsKey(key) && data[key] != null && Convert.ToBoolean(data[key]); }
        private static string NestedTextValue(Dictionary<string, object> data, string parent, string key) { return data.ContainsKey(parent) && data[parent] is Dictionary<string, object> ? TextValue((Dictionary<string, object>)data[parent], key, "") : ""; }
        private static string Number(Dictionary<string, object> data, string parent, string key) { return NestedTextValue(data, parent, key); }
        private static string OperationName(string value) { switch (value) { case "start": return "启动"; case "stop": return "停止"; case "restart": return "重启"; case "repair": return "修复"; case "install": return "安装 / 更新"; case "doctor": return "诊断"; default: return value; } }
        private static string StatusName(string value) { switch ((value ?? "").ToUpperInvariant()) { case "RUNNING": return "运行中"; case "STOPPED": return "已停止"; case "HEALTHY": return "健康"; case "RECOVERING": return "恢复中"; case "BACKOFF": return "等待重试"; case "MISSING": return "缺失"; default: return value; } }
    }
}

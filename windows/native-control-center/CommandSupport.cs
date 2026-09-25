using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.IO;
using System.Linq;
using System.Text;
using System.Web.Script.Serialization;

namespace AshareAI.NativeControlCenter
{
    internal static class CommandLine
    {
        private static readonly HashSet<string> Commands = new HashSet<string>(StringComparer.OrdinalIgnoreCase)
        {
            "install", "start", "stop", "restart", "repair", "status", "doctor", "open", "logs", "help", "version"
        };

        public static bool IsCliRequest(string[] args)
        {
            if (args == null || args.Length == 0) return false;
            var first = args[0];
            return IsCommand(first) ||
                String.Equals(first, "--cli", StringComparison.OrdinalIgnoreCase) ||
                String.Equals(first, "/cli", StringComparison.OrdinalIgnoreCase) ||
                String.Equals(first, "--help", StringComparison.OrdinalIgnoreCase) ||
                String.Equals(first, "-h", StringComparison.OrdinalIgnoreCase) ||
                String.Equals(first, "/?", StringComparison.OrdinalIgnoreCase);
        }

        public static bool IsCommand(string value)
        {
            return !String.IsNullOrEmpty(value) && Commands.Contains(value);
        }
    }

    internal static class ConsoleCommand
    {
        private static readonly JavaScriptSerializer Json = new JavaScriptSerializer();

        public static int Run(string[] args, bool requireCommand)
        {
            try
            {
                var request = CliRequest.Parse(args, requireCommand);
                if (request.ShowHelp || request.Operation == "help")
                {
                    Console.Out.WriteLine(HelpText());
                    return request.MissingCommand ? 2 : 0;
                }
                if (request.Operation == "version")
                {
            Console.Out.WriteLine("AshareAI Native Control Center 2.2.0");
                    return 0;
                }
                var options = Options.Parse(request.OptionArgs.ToArray());
                var controller = EmbeddedAssets.ExtractController(options.RuntimeRoot);
                if (request.Operation == "logs")
                    return PrintLogs(options, request.TailLines);
                if (request.Operation == "open")
                    return OpenWeb(controller, options);
                return RunController(controller, options, request);
            }
            catch (Exception error)
            {
                Console.Error.WriteLine("错误：" + error.Message);
                return 1;
            }
        }

        public static string HelpText()
        {
            return String.Join(Environment.NewLine, new[]
            {
                "AshareAI 本机运行管理器命令行",
                "",
                "用法:",
                "  AshareAI.NativeControlCenter.Cli.exe <命令> [选项]",
                "  AshareAI.NativeControlCenter.exe --cli <命令> [选项]",
                "",
                "命令:",
                "  install       安装或更新本机运行依赖",
                "  start         启动 PostgreSQL、Redis、API 和单一 job-worker",
                "  stop          停止本机运行进程",
                "  restart       重启本机运行进程",
                "  repair        修复端口、看门狗任务等本机配置",
                "  status        输出当前状态；加 --json 输出 JSON",
                "  doctor        运行诊断；加 --json 输出 JSON",
                "  open          用默认浏览器打开 Web 页面",
                "  logs          输出最近的看门狗日志",
                "",
                "常用选项:",
                "  --root <path>                 指定运行目录，默认管理器目录下的 runtime",
                "  --source-root <path>          指定应用载荷目录",
                "  --admin-username <name>       首次安装管理员用户名",
                "  --admin-password <password>   首次安装管理员密码",
                "  --tail <lines>                logs 命令输出行数，默认 200"
            });
        }

        private static int RunController(string controller, Options options, CliRequest request)
        {
            var arguments = new List<string>
            {
                "-NoLogo", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                "-File", controller,
                "-Command", request.Operation,
                "-Root", options.RuntimeRoot,
                "-SourceRoot", options.SourceRoot
            };
            if (request.Json) arguments.Add("-Json");
            if (request.Operation == "status") arguments.Add("-Fast");
            if (request.NoWatchdog) arguments.Add("-NoWatchdog");
            if (!String.IsNullOrEmpty(request.AdminUsername))
            {
                arguments.Add("-AdminUsername");
                arguments.Add(request.AdminUsername);
            }
            if (!String.IsNullOrEmpty(request.AdminPassword))
            {
                arguments.Add("-AdminPassword");
                arguments.Add(request.AdminPassword);
            }
            return RunProcess("powershell.exe", arguments);
        }

        private static int PrintLogs(Options options, int lines)
        {
            var logPath = Path.Combine(options.RuntimeRoot, "logs", "watchdog.log");
            if (!File.Exists(logPath))
            {
                Console.Out.WriteLine("暂无看门狗日志：" + logPath);
                return 0;
            }
            var content = File.ReadAllLines(logPath, Encoding.UTF8);
            foreach (var line in content.Skip(Math.Max(0, content.Length - lines)))
                Console.Out.WriteLine(line);
            return 0;
        }

        private static int OpenWeb(string controller, Options options)
        {
            var output = new StringBuilder();
            var code = RunProcess("powershell.exe", new[]
            {
                "-NoLogo", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                "-File", controller, "-Command", "status", "-Root", options.RuntimeRoot,
                "-SourceRoot", options.SourceRoot, "-Json", "-Fast"
            }, output, null);
            if (code != 0) return code;
            var report = Json.DeserializeObject(output.ToString()) as Dictionary<string, object>;
            if (report == null || !report.ContainsKey("ports") || !(report["ports"] is Dictionary<string, object>))
                throw new InvalidOperationException("状态响应中缺少端口信息");
            var ports = (Dictionary<string, object>)report["ports"];
            var api = Convert.ToString(ports["api"]);
            Process.Start(new ProcessStartInfo("http://127.0.0.1:" + api + "/") { UseShellExecute = true });
            return 0;
        }

        private static int RunProcess(string fileName, IEnumerable<string> arguments)
        {
            return RunProcess(fileName, arguments, null, null);
        }

        private static int RunProcess(string fileName, IEnumerable<string> arguments, StringBuilder capturedOutput, StringBuilder capturedError)
        {
            var start = new ProcessStartInfo(fileName, String.Join(" ", arguments.Select(Quote)))
            {
                UseShellExecute = false,
                CreateNoWindow = true,
                RedirectStandardOutput = true,
                RedirectStandardError = true
            };
            using (var process = new Process())
            {
                process.StartInfo = start;
                process.OutputDataReceived += delegate(object sender, DataReceivedEventArgs e)
                {
                    if (e.Data == null) return;
                    if (capturedOutput != null) capturedOutput.AppendLine(e.Data);
                    else Console.Out.WriteLine(e.Data);
                };
                process.ErrorDataReceived += delegate(object sender, DataReceivedEventArgs e)
                {
                    if (e.Data == null) return;
                    if (capturedError != null) capturedError.AppendLine(e.Data);
                    else Console.Error.WriteLine(e.Data);
                };
                process.Start();
                process.BeginOutputReadLine();
                process.BeginErrorReadLine();
                process.WaitForExit();
                return process.ExitCode;
            }
        }

        internal static string Quote(string value)
        {
            if (value == null) return "\"\"";
            return "\"" + value.Replace("\"", "\\\"") + "\"";
        }
    }

    internal sealed class CliRequest
    {
        public string Operation;
        public bool ShowHelp;
        public bool MissingCommand;
        public bool Json;
        public bool NoWatchdog;
        public string AdminUsername;
        public string AdminPassword;
        public int TailLines = 200;
        public readonly List<string> OptionArgs = new List<string>();

        public static CliRequest Parse(string[] args, bool requireCommand)
        {
            var request = new CliRequest();
            var index = 0;
            if (args.Length > 0 && (String.Equals(args[0], "--cli", StringComparison.OrdinalIgnoreCase) || String.Equals(args[0], "/cli", StringComparison.OrdinalIgnoreCase)))
                index = 1;
            for (; index < args.Length; index++)
            {
                var value = args[index];
                var lower = value.ToLowerInvariant();
                if (CommandLine.IsCommand(value) && request.Operation == null)
                {
                    request.Operation = lower;
                    continue;
                }
                if (lower == "--help" || lower == "-h" || lower == "/?")
                {
                    request.ShowHelp = true;
                    continue;
                }
                if (lower == "--json" || lower == "-j")
                {
                    request.Json = true;
                    continue;
                }
                if (lower == "--no-watchdog")
                {
                    request.NoWatchdog = true;
                    continue;
                }
                if ((lower == "--source-root" || lower == "-s" || lower == "--root" || lower == "-r") && index + 1 < args.Length)
                {
                    request.OptionArgs.Add(value);
                    request.OptionArgs.Add(args[++index]);
                    continue;
                }
                if (lower == "--admin-username" && index + 1 < args.Length)
                {
                    request.AdminUsername = args[++index];
                    continue;
                }
                if (lower == "--admin-password" && index + 1 < args.Length)
                {
                    request.AdminPassword = args[++index];
                    continue;
                }
                if (lower == "--tail" && index + 1 < args.Length)
                {
                    request.TailLines = Math.Max(1, Int32.Parse(args[++index]));
                    continue;
                }
                throw new ArgumentException("未知命令行参数：" + value);
            }
            if (request.Operation == null)
            {
                request.MissingCommand = requireCommand && !request.ShowHelp;
                request.ShowHelp = true;
            }
            return request;
        }
    }
}

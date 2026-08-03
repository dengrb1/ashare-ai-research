namespace AshareAI.NativeControlCenter
{
    internal static class CliProgram
    {
        private static int Main(string[] args)
        {
            return ConsoleCommand.Run(args, true);
        }
    }
}

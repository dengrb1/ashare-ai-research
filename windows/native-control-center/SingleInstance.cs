using System;
using System.Threading;
using System.Runtime.InteropServices;

namespace AshareAI.NativeControlCenter
{
    internal sealed class SingleInstance : IDisposable
    {
        private const string MutexName = @"Local\AshareAI.NativeControlCenter";
        private const string WindowTitle = "AshareAI 本机运行管理器";
        private const int ShowRestore = 9;
        private Mutex mutex;
        private bool ownsMutex;

        public bool TryAcquire()
        {
            bool createdNew;
            mutex = new Mutex(true, MutexName, out createdNew);
            if (createdNew)
            {
                ownsMutex = true;
                return true;
            }

            mutex.Dispose();
            mutex = null;
            ActivateExistingWindow();
            return false;
        }

        public void Dispose()
        {
            if (mutex == null) return;
            if (ownsMutex)
            {
                try { mutex.ReleaseMutex(); } catch (ApplicationException) { }
                ownsMutex = false;
            }
            mutex.Dispose();
            mutex = null;
        }

        private static void ActivateExistingWindow()
        {
            for (var attempt = 0; attempt < 6; attempt++)
            {
                var handle = FindWindow(null, WindowTitle);
                if (handle != IntPtr.Zero)
                {
                    ShowWindowAsync(handle, ShowRestore);
                    SetForegroundWindow(handle);
                    return;
                }
                Thread.Sleep(50);
            }
        }

        [DllImport("user32.dll", CharSet = CharSet.Unicode)]
        private static extern IntPtr FindWindow(string className, string windowName);

        [DllImport("user32.dll")]
        private static extern bool ShowWindowAsync(IntPtr handle, int command);

        [DllImport("user32.dll")]
        private static extern bool SetForegroundWindow(IntPtr handle);
    }
}

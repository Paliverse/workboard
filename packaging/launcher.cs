// npm's Windows launcher: copied to <npm prefix>\workboard.exe and wb.exe next to the .cmd shims, so shells
// and CreateProcess callers get a real exe (no cmd.exe between them and the arguments). It runs the platform
// package's workboard.exe with the raw command-line tail, untouched. Built by `packaging/build.py binary`
// with the csc.exe of .NET Framework 4.x that ships with Windows; AnyCPU serves x64 and arm64.
using System;
using System.IO;
using System.Runtime.InteropServices;
using System.Text;

static class Launcher
{
    [StructLayout(LayoutKind.Sequential, CharSet = CharSet.Unicode)]
    struct StartupInfo
    {
        public int cb;
        public string reserved, desktop, title;
        public int x, y, xSize, ySize, xCountChars, yCountChars, fillAttribute, flags;
        public short showWindow, reserved2;
        public IntPtr reserved3, stdInput, stdOutput, stdError;
    }

    [StructLayout(LayoutKind.Sequential)]
    struct ProcessInfo { public IntPtr process, thread; public int processId, threadId; }

    // JOBOBJECT_EXTENDED_LIMIT_INFORMATION; Sequential layout gives the native padding on 32- and 64-bit.
    [StructLayout(LayoutKind.Sequential)]
    struct JobLimits
    {
        public long perProcessUserTimeLimit, perJobUserTimeLimit;
        public int limitFlags;
        public UIntPtr minimumWorkingSetSize, maximumWorkingSetSize;
        public int activeProcessLimit;
        public UIntPtr affinity;
        public int priorityClass, schedulingClass;
        public ulong readOperations, writeOperations, otherOperations, readBytes, writeBytes, otherBytes;
        public UIntPtr processMemoryLimit, jobMemoryLimit, peakProcessMemoryUsed, peakJobMemoryUsed;
    }

    delegate bool CtrlHandler(int ctrlType);

    [DllImport("kernel32.dll", CharSet = CharSet.Unicode)] static extern IntPtr GetCommandLineW();
    [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
    static extern bool CreateProcessW(string application, StringBuilder commandLine, IntPtr processAttributes,
        IntPtr threadAttributes, bool inheritHandles, int flags, IntPtr environment, string directory,
        ref StartupInfo startupInfo, out ProcessInfo processInfo);
    [DllImport("kernel32.dll")] static extern IntPtr GetStdHandle(int which);
    [DllImport("kernel32.dll")] static extern bool SetHandleInformation(IntPtr handle, int mask, int flags);
    [DllImport("kernel32.dll")] static extern bool SetConsoleCtrlHandler(CtrlHandler handler, bool add);
    [DllImport("kernel32.dll")] static extern int WaitForSingleObject(IntPtr handle, int milliseconds);
    [DllImport("kernel32.dll")] static extern bool GetExitCodeProcess(IntPtr process, out int exitCode);
    [DllImport("kernel32.dll")] static extern IntPtr CreateJobObjectW(IntPtr attributes, IntPtr name);
    [DllImport("kernel32.dll")] static extern bool SetInformationJobObject(IntPtr job, int infoClass, ref JobLimits info, int size);
    [DllImport("kernel32.dll")] static extern bool AssignProcessToJobObject(IntPtr job, IntPtr process);
    [DllImport("kernel32.dll")] static extern int ResumeThread(IntPtr thread);

    // A handler, not SetConsoleCtrlHandler(null, true): that flag would be inherited and the child would ignore Ctrl+C too.
    static readonly CtrlHandler IgnoreCtrl = ctrlType => true;

    static int Main()
    {
        string self = System.Reflection.Assembly.GetEntryAssembly().Location;
        string prefix = Path.GetDirectoryName(self);
        string arch = (Environment.GetEnvironmentVariable("PROCESSOR_ARCHITEW6432")
                       ?? Environment.GetEnvironmentVariable("PROCESSOR_ARCHITECTURE") ?? "").ToUpperInvariant() == "ARM64"
            ? "arm64" : "x64";
        string missing = null, target = null;
        // The platform package sits under the main package, or hoisted next to it.
        foreach (string a in new[] { arch, arch == "x64" ? "arm64" : "x64" })
            foreach (string root in new[] { @"node_modules\@paliverse\workboard\", "" })
            {
                string candidate = Path.Combine(prefix, root + @"node_modules\@paliverse\workboard-win32-" + a + @"\workboard\workboard.exe");
                if (File.Exists(candidate)) { target = candidate; goto found; }
                if (missing == null) missing = candidate;
            }
        Console.Error.WriteLine("workboard: " + missing + " is missing; reinstall with `npm install -g @paliverse/workboard`, or delete " + self);
        return 1;
    found:
        // argv[0] by the Windows rules: a quoted run up to the next quote, else up to the first space or tab.
        string line = Marshal.PtrToStringUni(GetCommandLineW());
        int end;
        if (line.StartsWith("\"")) { end = line.IndexOf('"', 1); end = end < 0 ? line.Length : end + 1; }
        else { end = line.IndexOfAny(new[] { ' ', '\t' }); if (end < 0) end = line.Length; }
        var commandLine = new StringBuilder("\"" + target + "\"" + line.Substring(end));

        var startup = new StartupInfo { cb = Marshal.SizeOf(typeof(StartupInfo)), flags = 0x100 /* STARTF_USESTDHANDLES */ };
        startup.stdInput = GetStdHandle(-10);
        startup.stdOutput = GetStdHandle(-11);
        startup.stdError = GetStdHandle(-12);
        foreach (IntPtr handle in new[] { startup.stdInput, startup.stdOutput, startup.stdError })
            SetHandleInformation(handle, 1, 1); // HANDLE_FLAG_INHERIT; fails harmlessly on null handles
        SetConsoleCtrlHandler(IgnoreCtrl, true);
        ProcessInfo child;
        if (!CreateProcessW(target, commandLine, IntPtr.Zero, IntPtr.Zero, true, 0x4 /* CREATE_SUSPENDED */,
                            IntPtr.Zero, null, ref startup, out child))
        {
            Console.Error.WriteLine("workboard: could not run " + target + ": " + new System.ComponentModel.Win32Exception().Message);
            return 1;
        }
        // Killing the launcher (a caller's timeout, mintty's Ctrl+C) closes the job and kills the child with it, so it
        // can't keep the caller's pipes open. Silent breakaway: the processes the child starts (the detached server of
        // `open`, the upgrade's runtime copy) stay outside the job and outlive the launcher. Without a job, carry on.
        IntPtr job = CreateJobObjectW(IntPtr.Zero, IntPtr.Zero);
        if (job != IntPtr.Zero)
        {
            var limits = new JobLimits { limitFlags = 0x2000 | 0x800 | 0x1000 }; // KILL_ON_JOB_CLOSE | BREAKAWAY_OK | SILENT_BREAKAWAY_OK
            if (SetInformationJobObject(job, 9 /* JobObjectExtendedLimitInformation */, ref limits, Marshal.SizeOf(typeof(JobLimits))))
                AssignProcessToJobObject(job, child.process);
        }
        ResumeThread(child.thread);
        WaitForSingleObject(child.process, -1);
        int code;
        GetExitCodeProcess(child.process, out code);
        return code;
    }
}

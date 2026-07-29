Option Explicit

' WScript has no console window. It starts the actual controller hidden so a
' scheduled topology sync can never steal focus from a full-screen program.
Dim fso, shell, projectRoot, controller, command, exitCode
Set fso = CreateObject("Scripting.FileSystemObject")
Set shell = CreateObject("WScript.Shell")
projectRoot = fso.GetParentFolderName(fso.GetParentFolderName(WScript.ScriptFullName))
controller = fso.BuildPath(projectRoot, "scripts\topology-controller.ps1")
command = "powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass -File """ & controller & """"

' Wait for the controller before WScript exits. Task Scheduler then sees one
' running instance, rather than launching overlapping hidden controllers when
' a Docker operation takes longer than its one-minute interval.
exitCode = shell.Run(command, 0, True)
WScript.Quit exitCode

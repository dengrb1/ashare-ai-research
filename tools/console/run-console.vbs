Option Explicit

Dim fso, shell, scriptDir, repoRoot, pythonw, appPath, command
Set fso = CreateObject("Scripting.FileSystemObject")
Set shell = CreateObject("WScript.Shell")

scriptDir = fso.GetAbsolutePathName(fso.GetParentFolderName(WScript.ScriptFullName))
repoRoot = fso.GetAbsolutePathName(fso.BuildPath(scriptDir, "..\.."))
pythonw = fso.BuildPath(repoRoot, ".venv\Scripts\pythonw.exe")
If Not fso.FileExists(pythonw) Then pythonw = "pythonw.exe"
appPath = fso.BuildPath(scriptDir, "console_app.py")

shell.CurrentDirectory = repoRoot
command = Chr(34) & pythonw & Chr(34) & " " & Chr(34) & appPath & Chr(34)
shell.Run command, 0, False

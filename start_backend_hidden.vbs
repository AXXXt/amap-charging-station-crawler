Option Explicit

Dim shell, fso, root, powershell, launcher, command, exitCode
Set shell = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")

root = fso.GetParentFolderName(WScript.ScriptFullName)
powershell = shell.ExpandEnvironmentStrings("%WINDIR%") & "\System32\WindowsPowerShell\v1.0\powershell.exe"
launcher = root & "\start_backend.ps1"
command = """" & powershell & """ -NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass -WindowStyle Hidden -File """ & launcher & """"

exitCode = shell.Run(command, 0, True)
WScript.Quit exitCode
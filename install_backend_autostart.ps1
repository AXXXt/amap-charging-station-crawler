# Install or update per-user API autostart. Administrator rights are not needed.
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$launcher = Join-Path $root "start_backend.ps1"
$hiddenLauncher = Join-Path $root "start_backend_hidden.vbs"
if (-not (Test-Path $launcher)) {
    throw "找不到启动脚本: $launcher"
}
if (-not (Test-Path $hiddenLauncher)) {
    throw "找不到无窗口启动器: $hiddenLauncher"
}

$startup = [Environment]::GetFolderPath("Startup")
$shortcutPath = Join-Path $startup "EVCS charging station API.lnk"
$wscript = Join-Path $env:WINDIR "System32\wscript.exe"

$shell = New-Object -ComObject WScript.Shell
$shortcut = $shell.CreateShortcut($shortcutPath)
$shortcut.TargetPath = $wscript
$shortcut.Arguments = "`"$hiddenLauncher`""
$shortcut.WorkingDirectory = $root
$shortcut.WindowStyle = 7
$shortcut.Description = "Start EVCS charging station API after Windows logon (hidden)"
$shortcut.Save()

# Startup launches immediately after logon. The scheduled task also re-checks
# every five minutes, so a crashed backend is restarted without another logon.
$taskName = "EVCS Charging Station API"
$taskUser = "$env:USERDOMAIN\$env:USERNAME"
$taskArgument = "`"$hiddenLauncher`""
$taskAction = New-ScheduledTaskAction -Execute $wscript -Argument $taskArgument -WorkingDirectory $root
$logonTrigger = New-ScheduledTaskTrigger -AtLogOn -User $taskUser
$watchTrigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(-1) `
    -RepetitionInterval (New-TimeSpan -Minutes 5) `
    -RepetitionDuration (New-TimeSpan -Days 3650)
$taskSettings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 5)
$taskPrincipal = New-ScheduledTaskPrincipal -UserId $taskUser `
    -LogonType Interactive -RunLevel Limited
Register-ScheduledTask -TaskName $taskName `
    -Action $taskAction `
    -Trigger @($logonTrigger, $watchTrigger) `
    -Settings $taskSettings `
    -Principal $taskPrincipal `
    -Force | Out-Null

# Start the backend immediately after installation without opening a console window.
& $wscript $hiddenLauncher

Write-Host "Installed startup shortcut: $shortcutPath"
Write-Host "Installed watcher task: $taskName"
Write-Host "Service URL: http://$([System.Net.Dns]::GetHostName()):8800"
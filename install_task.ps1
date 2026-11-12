<#
.SYNOPSIS
    Register a Windows task that uploads the next due video.

.DESCRIPTION
    The uploader uploads one video per day, 90 minutes before its 20:00-21:00
    publish slot. A single daily run would start too early (before the upload
    time), so this task repeats every 30 minutes through the evening window and
    uploads only when a video is actually due. The one-upload-per-day cap means
    the later runs do nothing.

    With -StartWhenAvailable, a missed run starts as soon as the PC is back on;
    if the publish time has already passed, the video is moved to the next free
    day instead of publishing late.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File .\install_task.ps1
    powershell -ExecutionPolicy Bypass -File .\install_task.ps1 -At 18:00
#>
param(
    [string]$At = "18:00",
    [int]$EveryMinutes = 30,
    [int]$WindowHours = 4,
    [string]$TaskName = "YouTube Auto Uploader"
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$main = Join-Path $root "main.py"
if (-not (Test-Path $main)) {
    throw "main.py not found next to this script: $root"
}

$python = (Get-Command python -ErrorAction Stop).Source
$pythonw = Join-Path (Split-Path $python) "pythonw.exe"
$exe = if (Test-Path $pythonw) { $pythonw } else { $python }
$arguments = "`"$main`" upload-next"

$action = New-ScheduledTaskAction `
    -Execute $exe `
    -Argument $arguments `
    -WorkingDirectory $root

# Daily anchor + repetition every $EveryMinutes for $WindowHours.
$trigger = New-ScheduledTaskTrigger -Daily -At $At
$repetition = (New-ScheduledTaskTrigger `
    -Once `
    -At $At `
    -RepetitionInterval (New-TimeSpan -Minutes $EveryMinutes) `
    -RepetitionDuration (New-TimeSpan -Hours $WindowHours)).Repetition
$trigger.Repetition = $repetition

$settings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit (New-TimeSpan -Hours 8) `
    -MultipleInstances IgnoreNew

$principal = New-ScheduledTaskPrincipal `
    -UserId "$env:USERDOMAIN\$env:USERNAME" `
    -LogonType Interactive `
    -RunLevel Limited

Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $action `
    -Trigger $trigger `
    -Settings $settings `
    -Principal $principal `
    -Description "Uploads the next due video from the YouTube auto-uploader plan (one per day)." `
    -Force | Out-Null

Write-Host "Task registered: $TaskName"
Write-Host "Runs every $EveryMinutes min from $At for $WindowHours h, as $exe"
Write-Host "Only one video is uploaded per day; later runs are no-ops."
Write-Host "Uploads log to: $(Join-Path $root 'data\uploader.log')"
Write-Host ""
Write-Host "Test it now:  Start-ScheduledTask -TaskName `"$TaskName`""
Write-Host "Inspect it:   Get-ScheduledTask -TaskName `"$TaskName`" | Get-ScheduledTaskInfo"
Write-Host "Remove it:    powershell -ExecutionPolicy Bypass -File .\uninstall_task.ps1"

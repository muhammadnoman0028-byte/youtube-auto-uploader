<#
.SYNOPSIS
    Register a daily Windows task that uploads a quota-sized batch of pending videos.

.DESCRIPTION
    YouTube allows roughly 6 uploads per day (10,000 API units, 1,600 per upload).
    The API quota resets at 00:00 Pacific = 13:00 Asia/Karachi. This task runs
    shortly after that reset and uploads up to $Limit pending videos that are
    already scheduled on the calendar, so a large backlog clears in days
    instead of months (the one-per-day task only handles the current day's slot).

    Uploads stay private with their scheduled publishAt time; the normal
    one-per-day task is unaffected and simply finds nothing due afterwards.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File .\install_bulk_task.ps1
    powershell -ExecutionPolicy Bypass -File .\install_bulk_task.ps1 -Limit 4 -At 13:15
#>
param(
    [string]$At = "13:10",
    [int]$Limit = 6,
    [string]$TaskName = "YouTube Bulk Uploader"
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
$arguments = "`"$main`" bulk-upload --limit $Limit"

$action = New-ScheduledTaskAction `
    -Execute $exe `
    -Argument $arguments `
    -WorkingDirectory $root

$trigger = New-ScheduledTaskTrigger -Daily -At $At

$settings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit (New-TimeSpan -Hours 6) `
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
    -Description "Uploads up to $Limit scheduled videos daily after the YouTube API quota reset." `
    -Force | Out-Null

Write-Host "Task registered: $TaskName"
Write-Host "Runs daily at $At, uploading up to $Limit videos."
Write-Host "Uploads log to: $(Join-Path $root 'data\uploader.log')"
Write-Host ""
Write-Host "Test it now:  Start-ScheduledTask -TaskName `"$TaskName`""
Write-Host "Inspect it:   Get-ScheduledTask -TaskName `"$TaskName`" | Get-ScheduledTaskInfo"
Write-Host "Remove it:    Unregister-ScheduledTask -TaskName `"$TaskName`" -Confirm:`$false"

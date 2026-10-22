param(
    [string]$TaskName = 'TaiwanImportCheck Official Data Weekly Update'
)

$ErrorActionPreference = 'Stop'
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$PythonPath = 'C:\Users\aaaee\AppData\Local\hermes\hermes-agent\venv\Scripts\python.exe'

if (-not (Test-Path -LiteralPath $PythonPath)) {
    throw 'Hermes Python runtime was not found. Edit PythonPath to the installed Python 3.11 executable.'
}

$Action = New-ScheduledTaskAction `
    -Execute $PythonPath `
    -Argument 'scripts\update_official_data.py --download' `
    -WorkingDirectory $ProjectRoot
$Trigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Sunday -At '3:00 AM'
$Principal = New-ScheduledTaskPrincipal `
    -UserId "$env:USERDOMAIN\$env:USERNAME" `
    -LogonType Interactive `
    -RunLevel Limited
$Settings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -ExecutionTimeLimit (New-TimeSpan -Hours 3) `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries

Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $Action `
    -Trigger $Trigger `
    -Principal $Principal `
    -Settings $Settings `
    -Description 'Weekly official-source fetch, validation, and PASS-only SQLite activation for TaiwanImportCheck.' `
    -Force | Out-Null

Write-Output "Scheduled task registered: $TaskName (Sunday 03:00, current user must be signed in)."

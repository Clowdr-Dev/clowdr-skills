# Registers the mailbox dispatcher as a scheduled task that starts at logon,
# runs without a window and restarts if it stops. Run in Windows PowerShell:
#
#   powershell -ExecutionPolicy Bypass -File <mailbox>\install-autostart.ps1
#   powershell -ExecutionPolicy Bypass -File <mailbox>\install-autostart.ps1 -Remove
#
# The task runs as the current user, so codex queue reaches that user's sessions.

param([switch]$Remove)

$TaskName = 'AgentMailboxDispatcher'

if ($Remove) {
    Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    "Removed scheduled task $TaskName."
    return
}

$python = (Get-Command python -ErrorAction Stop).Source
$pythonw = Join-Path (Split-Path $python) 'pythonw.exe'
if (-not (Test-Path $pythonw)) { throw "pythonw.exe not found next to $python" }
$codex = (Get-Command codex -ErrorAction Stop).Source
$dispatcher = Join-Path $PSScriptRoot 'dispatcher.py'

$action = New-ScheduledTaskAction -Execute $pythonw `
    -Argument "`"$dispatcher`" --codex `"$codex`"" -WorkingDirectory $PSScriptRoot
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$settings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -MultipleInstances IgnoreNew

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings `
    -Description 'Delivers agent mailbox messages to Codex sessions via codex queue.' -Force | Out-Null
Start-ScheduledTask -TaskName $TaskName
"Registered and started scheduled task $TaskName (log: $(Join-Path $PSScriptRoot 'dispatcher.log'))."

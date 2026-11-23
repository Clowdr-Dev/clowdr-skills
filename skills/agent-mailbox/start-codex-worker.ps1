# Starts an interactive Codex worker session in its own window. The session
# registers itself in the agent mailbox with its own Codex session ID and then
# sends the orchestrator a "started <token> codex:<id> cwd=..." message.
#
#   powershell -ExecutionPolicy Bypass -File <mailbox>\start-codex-worker.ps1 `
#       -Orchestrator claude:<session-id> -Project C:\path\to\project [-Label reviewer] [-DryRun]
#
# Prints one JSON line with the token. Wait for the mailbox message that
# contains that token to learn the worker's address. See ORCHESTRATOR.md.

param(
    [Parameter(Mandatory)][string]$Orchestrator,
    [Parameter(Mandatory)][string]$Project,
    [string]$Label = 'orchestrated worker',
    [ValidateSet('read-only', 'workspace-write')][string]$Sandbox = 'workspace-write',
    [switch]$DryRun
)

$ErrorActionPreference = 'Stop'
if ($Orchestrator -notmatch '^[a-z0-9_-]+:[A-Za-z0-9_.-]+$') { throw "Orchestrator must be a mailbox address like claude:<session-id>" }
if ($Label -match "[`"'`$]") { throw "Label must not contain quotes or `$" }
$Project = (Resolve-Path -LiteralPath $Project).Path
$mailboxDir = $PSScriptRoot
$token = 'orch-' + [guid]::NewGuid().ToString('N').Substring(0, 10)
$mb = "python $mailboxDir\mailbox.py"

# The prompt goes through one level of Windows command-line quoting, so it
# must not contain double quotes; single quotes are fine.
$prompt = "[Orchestrator-Start $token] You were started by the orchestrator $Orchestrator and work with it through the agent mailbox from now on. " +
    "1) Read $mailboxDir\AGENT-BRIEF.md. " +
    "2) Register with your own Codex session ID: $mb register --tool codex --session YOUR-SESSION-ID --label '$Label' --delivery codex-queue. " +
    "3) Report to the orchestrator: $mb send --from codex:YOUR-SESSION-ID --to $Orchestrator --body 'started $token codex:YOUR-SESSION-ID cwd=YOUR-WORKING-FOLDER'. " +
    "4) Then do nothing else and wait: further tasks arrive in this chat as labeled mailbox messages."

$argumentLine = "-C `"$Project`" -s $Sandbox --add-dir `"$mailboxDir`" `"$prompt`""

if ($DryRun) {
    [pscustomobject]@{ token = $token; project = $Project; orchestrator = $Orchestrator; command = "codex $argumentLine" } |
        ConvertTo-Json -Compress
    return
}

Start-Process -FilePath codex -ArgumentList $argumentLine
[pscustomobject]@{ token = $token; project = $Project; orchestrator = $Orchestrator; started = (Get-Date).ToUniversalTime().ToString('o') } |
    ConvertTo-Json -Compress

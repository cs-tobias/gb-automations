# Starts GB Automations. The GB Automations program (GbAutomations.ps1) runs
# this with -NoLiveLog when it opens; run on its own (Start-GbAutomations.cmd)
# it's the same thing in a plain console, for troubleshooting.
#
# It:
#   1. starts Ollama (AI tagging) if it isn't answering,
#   2. starts Docker Desktop in the background (no Docker window) if it's off,
#   3. makes sure the NAS helper is installed (a Docker upgrade can drop it),
#   4. runs `docker compose up -d` (starts whatever isn't running; never
#      rebuilds),
#   5. waits until the app answers,
#   6. then (unless -NoLiveLog) stays open as the app's live log.
#
# Safe to run any time: every step is a no-op when that part is already up.
# Closing the window only closes the log view. The app keeps running.
#
# -NoLiveLog: just start, then exit (0 = started, 1 = couldn't). The program
#   shows the log itself.
# -AtLogon: what the sign-in shortcut of installs from before the program
#   runs. It opens the program instead.

[CmdletBinding()]
param([switch]$AtLogon, [switch]$NoLiveLog)

. (Join-Path $PSScriptRoot 'GbMove.Common.ps1')

$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$liveLogTitle = 'GB Automations - live log'

if ($AtLogon) {
    Start-GbProgram $repoRoot
    exit 0
}

function Show-Problem([string]$Message, [string]$LogPath) {
    Write-Host ''
    Write-Host "PROBLEM: $Message" -ForegroundColor Red
    Write-Note 'Help: docs\move-to-new-pc.md (Troubleshooting).'
    if ($LogPath) { Write-Note "Log of this start: $LogPath" }
    if (-not $NoLiveLog) { Read-Host 'Press Enter to close this window' | Out-Null }
}

function Show-LiveLog([bool]$Answering) {
    # Streams the api container's log into this window until it's closed.
    # docker writes straight to the console (no PowerShell pipe), which keeps
    # the app's colours and the Norwegian letters intact.
    $Host.UI.RawUI.WindowTitle = $liveLogTitle
    Write-Host ''
    if ($Answering) {
        Write-Host 'GB Automations is running.' -ForegroundColor Green
    } else {
        Write-Attention 'The app has started but is not answering yet. The log below shows why.'
    }
    Write-Note "Status page: $script:GbStatusUrl"
    Write-Note 'Closing this window does NOT stop the app. The icon opens it again.'
    Write-Host ('-' * 78) -ForegroundColor DarkGray

    $ErrorActionPreference = 'Continue'
    $since = $null
    $pausedShown = $false
    while ($true) {
        $logArgs = @('compose', 'logs', '--follow', '--no-log-prefix')
        # First connect: the recent past for context. After a restart (an
        # update, a crash, Docker restarting) pick up exactly where it stopped.
        if ($since) { $logArgs += @('--since', $since) } else { $logArgs += @('--tail', '150') }
        $logArgs += 'api'
        $startedAt = Get-Date
        & docker @logArgs
        if (((Get-Date) - $startedAt).TotalSeconds -gt 10) {
            $since = [DateTime]::UtcNow.ToString('yyyy-MM-ddTHH:mm:ssZ')
            $pausedShown = $false
        }
        # Said once per outage, not every retry.
        if (-not $pausedShown) {
            Write-Host ''
            Write-Host '-- The app stopped or is restarting. The log continues as soon as it is back. --' -ForegroundColor DarkGray
            $pausedShown = $true
        }
        Start-Sleep -Seconds 5
    }
}

# A second double-click shouldn't stack a second log window.
$existing = Get-Process -Name 'powershell' -ErrorAction SilentlyContinue |
    Where-Object { $_.Id -ne $PID -and $_.MainWindowTitle -eq $liveLogTitle } |
    Select-Object -First 1

# Run by the program there is no console window to title.
if (-not $NoLiveLog) { $Host.UI.RawUI.WindowTitle = 'GB Automations - starting' }
$logPath = Start-GbLog 'start'
$answering = $false
try {
    Set-Location -LiteralPath $repoRoot
    Write-Host 'GB Automations - starting' -ForegroundColor White
    $envMap = Read-DotEnv (Join-Path $repoRoot '.env')

    Write-Step 'AI tagging (Ollama)'
    # Not fatal: emails still sync without tags, and the status page shows it.
    if (Start-Ollama -TimeoutSec 30) { Write-Ok 'Ollama is running' }
    else { Write-Attention 'Ollama is not running - emails sync without tags until it is.' }

    Write-Step 'Docker'
    Wait-DockerEngine
    Write-Ok 'Docker is running'

    if (Test-EnvTrue $envMap 'SYNC_NAS_FOLDERS') {
        Write-Step 'NAS helper'
        Install-CifsUtils
        Write-Ok 'Present'
    }

    Write-Step 'GB Automations'
    if ((Invoke-Native -Exe 'docker' -Arguments @('compose', 'up', '-d') -AllowFail) -ne 0) {
        throw 'Docker could not start the app (see the lines above). A "mount" error means the NAS: is this PC on the office network, and does the folder in NAS_CIFS_DEVICE (.env) still exist on the NAS?'
    }
    # Not fatal past this point: if the app doesn't answer, its own log (shown
    # next) is the fastest way to see why.
    $deadline = (Get-Date).AddMinutes(3)
    while (-not ($answering = Test-AppHealthy)) {
        if ((Get-Date) -gt $deadline) { break }
        Start-Sleep -Seconds 3
    }
    if ($answering) { Write-Ok 'Running' }
    elseif ($NoLiveLog) { Write-Attention 'The app has started but is not answering yet. The log shows why.' }
} catch {
    Stop-GbLog
    Show-Problem $_.Exception.Message $logPath
    exit 1
}
# The live log runs for as long as the window is open: keep it out of the
# start log, which is meant to record just this start.
Stop-GbLog
if ($NoLiveLog) { exit 0 }

if ($existing) {
    Write-Ok 'The live log is already open in another window - switching to it.'
    try { (New-Object -ComObject WScript.Shell).AppActivate($existing.Id) | Out-Null } catch { }
    Start-Sleep -Seconds 3
    exit 0
}

Show-LiveLog $answering

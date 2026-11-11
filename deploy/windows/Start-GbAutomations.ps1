# The everyday "GB Automations" desktop icon, and (with -AtLogon) the
# automatic start at Windows sign-in. Both are created by Install.ps1.
#
# Normally nothing is needed: the sign-in autostart already ran this. The icon
# is the "is it running? make it run, then show me" button. It's safe to click
# any time - every step is a no-op when that part is already up:
#   1. start Ollama (AI tagging) if it isn't answering,
#   2. start Docker Desktop if it's closed,
#   3. make sure the NAS helper is installed (a Docker upgrade can drop it),
#   4. `docker compose up -d` (starts whatever isn't running; never rebuilds),
#   5. wait until the app answers, then open the status page.
#
# -AtLogon: silent, no browser. Only a message box if something fails.

[CmdletBinding()]
param([switch]$AtLogon)

. (Join-Path $PSScriptRoot 'GbMove.Common.ps1')

$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$logPath = Start-GbLog 'start'

function Show-Problem([string]$Message) {
    $more = 'Help: docs\move-to-new-pc.md (Troubleshooting).'
    if ($logPath) { $more += " Log: $logPath" }
    if ($AtLogon) {
        Add-Type -AssemblyName PresentationFramework
        [Windows.MessageBox]::Show("GB Automations could not start.`n`n$Message`n`nDouble-click the GB Automations icon on the desktop to try again and see details.`n`n$more", 'GB Automations', 'OK', 'Warning') | Out-Null
    } else {
        Write-Host ''
        Write-Host "PROBLEM: $Message" -ForegroundColor Red
        Write-Note $more
        Read-Host 'Press Enter to close this window' | Out-Null
    }
}

try {
    Set-Location -LiteralPath $repoRoot
    if (-not $AtLogon) { Write-Host 'GB Automations - starting (this window closes by itself)' -ForegroundColor White }
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
    $deadline = (Get-Date).AddMinutes(3)
    while (-not (Test-AppHealthy)) {
        if ((Get-Date) -gt $deadline) { throw 'The app started but does not answer on http://localhost:8000.' }
        Start-Sleep -Seconds 3
    }
    Write-Ok 'Running'

    if (-not $AtLogon) { Start-Process $script:GbStatusUrl }
} catch {
    Show-Problem $_.Exception.Message
    exit 1
} finally {
    Stop-GbLog
}

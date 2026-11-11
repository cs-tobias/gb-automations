# Packs this PC's GB Automations into one encrypted move folder for a new PC.
#
# Run it on the OLD PC (double-click Export-ForMove.cmd). It:
#   1. stops the app (api + tunnel) so nothing changes while it copies,
#   2. dumps the database, and bundles it with .env, the Google key and the
#      exact code version into payload.gbmove (password-encrypted),
#   3. writes a GB-Automations-Move-<date> folder to the Desktop with
#      Install.cmd next to the payload,
#   4. switches the app OFF on this PC (docker compose down, data kept), so the
#      two PCs can never run at the same time.
#
# -TestOnly makes a move folder WITHOUT stopping or switching off anything
# (pg_dump is a consistent snapshot), for trying the restore with
# Install.ps1 -RestoreTestOnly.
#
# See docs/move-to-new-pc.md for the full picture.

[CmdletBinding()]
param(
    [switch]$TestOnly,
    [string]$OutputRoot = [Environment]::GetFolderPath('Desktop'),
    [switch]$Yes
)

. (Join-Path $PSScriptRoot 'GbMove.Common.ps1')

$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$logPath = Start-GbLog 'export'
$stopped = $false
$staging = $null
$exportFailed = $false

try {
    Set-Location -LiteralPath $repoRoot
    Write-Host 'GB Automations - export for moving to a new PC' -ForegroundColor White
    if ($TestOnly) { Write-Attention 'TEST MODE: the app keeps running on this PC.' }

    Write-Step 'Checking this folder has everything'
    foreach ($p in @('.env', 'secrets\gcp-service-account.json', 'docker-compose.yml', '.git')) {
        if (-not (Test-Path -LiteralPath (Join-Path $repoRoot $p))) { throw "Missing $p in $repoRoot - run this from the gb-automations folder that runs the app." }
    }
    if (-not (Get-Command git -ErrorAction SilentlyContinue)) { throw 'Git is not installed on this PC.' }
    $envMap = Read-DotEnv (Join-Path $repoRoot '.env')
    $pgUser = Get-EnvValue $envMap 'POSTGRES_USER' 'gb'
    $pgDb = Get-EnvValue $envMap 'POSTGRES_DB' 'gb'
    Write-Ok 'Found .env, Google key and code'

    $dirty = Get-NativeOutput -Exe 'git' -Arguments @('status', '--porcelain', '--untracked-files=no')
    if ($dirty) {
        Write-Attention 'These files have changes that are not committed - they will NOT move:'
        $dirty -split "`n" | ForEach-Object { Write-Note $_ }
        if (-not $Yes -and -not (Confirm-YesNo 'Continue anyway?')) { throw 'Stopped. Commit (or discard) the changes, then run this again.' }
    }

    Write-Step 'Starting Docker'
    Wait-DockerEngine
    Write-Ok 'Docker is running'

    $password = Read-NewPassword

    Write-Step 'Reading the database'
    Invoke-Native -Exe 'docker' -Arguments @('compose', 'up', '-d', 'db') | Out-Null
    $db = New-ComposeDbTarget
    Wait-Postgres $db $pgUser $pgDb
    $commit = Get-NativeOutput -Exe 'git' -Arguments @('rev-parse', 'HEAD')
    $alembic = Invoke-Psql $db $pgUser $pgDb 'select version_num from alembic_version'
    $frameEpoch = Invoke-Psql $db $pgUser $pgDb "select extract(epoch from updated_at)::bigint from oauth_tokens where provider = 'frame'" -AllowFail
    $frameAgeDays = $null
    if ($frameEpoch -match '^\d+$') {
        $frameAgeDays = [Math]::Round(([DateTimeOffset]::UtcNow.ToUnixTimeSeconds() - [long]$frameEpoch) / 86400.0, 1)
    }
    $countsBefore = Get-TableCounts $db $pgUser $pgDb
    Write-Ok "Code version $($commit.Substring(0, 7)), database migration $alembic"
    if (Test-EnvTrue $envMap 'SYNC_FRAME') {
        if ($null -eq $frameAgeDays) {
            Write-Attention 'No Frame sign-in stored - Frame will need a sign-in on the new PC.'
        } elseif ($frameAgeDays -gt 12) {
            Write-Attention "Frame's sign-in was last refreshed $frameAgeDays days ago. It expires after 14 days, so plan a Frame sign-in on the new PC (the status page will say so)."
        } else {
            Write-Ok "Frame sign-in is fresh ($frameAgeDays days old)"
        }
    }

    if (-not $TestOnly) {
        Write-Step 'Stopping the app on this PC'
        Invoke-Native -Exe 'docker' -Arguments @('compose', 'stop', 'api', 'cloudflared') | Out-Null
        $stopped = $true
        # Counts again: the worker may have written rows between the first read
        # and the stop. The manifest must describe the dump exactly.
        $countsBefore = Get-TableCounts $db $pgUser $pgDb
        Write-Ok 'App and tunnel stopped (database still running for the copy)'
    }

    Write-Step 'Copying the database'
    $stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
    $staging = Join-Path $env:TEMP "gb-move-export-$stamp"
    $payloadDir = Join-Path $staging 'payload'
    New-Item -ItemType Directory -Force -Path $payloadDir | Out-Null
    # pg_dump writes inside the container and `docker compose cp` copies the
    # file out: a PowerShell `>` redirect would re-encode the binary dump.
    Invoke-Native -Exe 'docker' -Arguments @('compose', 'exec', '-T', 'db', 'pg_dump', '-U', $pgUser, '-d', $pgDb, '-Fc', '-f', '/tmp/gb.dump') | Out-Null
    Invoke-Native -Exe 'docker' -Arguments @('compose', 'exec', '-T', 'db', 'pg_restore', '-l', '/tmp/gb.dump') -Quiet | Out-Null
    Invoke-Native -Exe 'docker' -Arguments @('compose', 'cp', 'db:/tmp/gb.dump', (Join-Path $payloadDir 'gb.dump')) | Out-Null
    Invoke-Native -Exe 'docker' -Arguments @('compose', 'exec', '-T', 'db', 'rm', '-f', '/tmp/gb.dump') -AllowFail | Out-Null
    $dumpSize = (Get-Item -LiteralPath (Join-Path $payloadDir 'gb.dump')).Length
    if ($dumpSize -lt 1024) { throw 'The database copy is suspiciously small - stopping.' }
    Write-Ok ("Database copied ({0:N1} MB)" -f ($dumpSize / 1MB))

    Write-Step 'Packing settings, Google key and code'
    Copy-Item -LiteralPath (Join-Path $repoRoot '.env') -Destination (Join-Path $payloadDir 'env')
    Copy-Item -LiteralPath (Join-Path $repoRoot 'secrets\gcp-service-account.json') -Destination (Join-Path $payloadDir 'gcp-service-account.json')
    Invoke-Native -Exe 'git' -Arguments @('bundle', 'create', (Join-Path $payloadDir 'repo.bundle'), '--all') -Quiet | Out-Null
    $manifest = [ordered]@{
        format             = 1
        created_utc        = [DateTime]::UtcNow.ToString('o')
        source_computer    = $env:COMPUTERNAME
        commit             = $commit
        alembic_version    = $alembic
        postgres_user      = $pgUser
        postgres_db        = $pgDb
        frame_token_age_days = $frameAgeDays
        sync_frame         = (Test-EnvTrue $envMap 'SYNC_FRAME')
        test_only          = [bool]$TestOnly
        counts             = $countsBefore
    }
    ($manifest | ConvertTo-Json -Depth 4) | Set-Content -LiteralPath (Join-Path $payloadDir 'manifest.json') -Encoding ASCII
    $zip = Join-Path $staging 'payload.zip'
    Compress-Archive -Path (Join-Path $payloadDir '*') -DestinationPath $zip -CompressionLevel Optimal

    $folderName = "GB-Automations-Move-$(Get-Date -Format 'yyyy-MM-dd')"
    if ($TestOnly) { $folderName += '-TEST' }
    $outDir = Join-Path $OutputRoot $folderName
    if (Test-Path -LiteralPath $outDir) { $outDir = "$outDir-$(Get-Date -Format 'HHmmss')" }
    New-Item -ItemType Directory -Force -Path $outDir | Out-Null
    Write-Note 'Encrypting (a few seconds)...'
    Protect-GbPayload $zip (Join-Path $outDir 'payload.gbmove') $password
    foreach ($f in @('Install.cmd', 'Install.ps1', 'GbMove.Common.ps1')) {
        Copy-Item -LiteralPath (Join-Path $PSScriptRoot $f) -Destination $outDir
    }
    @(
        'GB Automations - move folder',
        '',
        'On the NEW PC: copy this whole folder there (or open it from the USB stick),',
        'double-click Install.cmd and type the password you chose.',
        '',
        'The old PC has been switched off. Never run both at the same time.',
        'Full guide: docs/move-to-new-pc.md in the gb-automations code.',
        '',
        'This folder contains every password the system uses (encrypted).',
        'Delete it from the USB stick once the new PC is running.'
    ) | Set-Content -LiteralPath (Join-Path $outDir 'README.txt') -Encoding ASCII
    Write-Ok "Move folder ready: $outDir"

    if (-not $TestOnly) {
        Write-Step 'Switching the app off on this PC'
        # `down` (never `down -v`) removes the containers so Docker can't
        # auto-restart them after a reboot, but keeps the database volume as
        # the rollback copy.
        Invoke-Native -Exe 'docker' -Arguments @('compose', 'down') | Out-Null
        $stopped = $false
        Write-Ok 'Switched off. The data stays on this PC as a backup.'
    }

    Write-Host ''
    Write-Host 'DONE.' -ForegroundColor Green
    Write-Note "1. Copy the folder '$outDir' to a USB stick."
    Write-Note '2. On the new PC, open it and double-click Install.cmd.'
    if (-not $TestOnly) {
        Write-Note ''
        Write-Note 'Changed your mind? To switch this PC back on (only while the new PC is OFF):'
        Write-Note "   cd `"$repoRoot`"; docker compose up -d"
    }
} catch {
    Write-Host ''
    Write-Host "FAILED: $($_.Exception.Message)" -ForegroundColor Red
    if ($stopped) {
        Write-Note 'Starting the app again on this PC so nothing is left half-off...'
        Invoke-Native -Exe 'docker' -Arguments @('compose', 'start', 'api', 'cloudflared') -AllowFail | Out-Null
    }
    if ($logPath) { Write-Note "Log: $logPath" }
    $exportFailed = $true
} finally {
    if ($staging -and (Test-Path -LiteralPath $staging)) {
        Remove-Item -LiteralPath $staging -Recurse -Force -ErrorAction SilentlyContinue
    }
    Stop-GbLog
}
if ($exportFailed) { exit 1 }

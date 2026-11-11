# Installs GB Automations on this PC from a move folder made by Export-ForMove.
#
# Double-click Install.cmd in the move folder. Every step checks whether it is
# already done, so if Windows needs a restart halfway, restart and double-click
# Install.cmd again - it carries on where it stopped.
#
# What it does: Windows features for Docker (WSL2), installs Git, Docker
# Desktop and Ollama, sets up the NAS helper, unpacks the code, settings and
# database from the encrypted move file, starts the app, catches up on missed
# email, and puts a "GB Automations" icon on the desktop.
#
# -RestoreTestOnly only proves the move file is good: it restores the database
# copy into a throwaway container, compares row counts and removes it. It
# installs nothing and never touches a running app.
#
# Full guide: docs/move-to-new-pc.md

[CmdletBinding()]
param(
    [string]$InstallDir = 'C:\Automatisering\gb-automations',
    [switch]$RestoreTestOnly,
    [switch]$NoPause
)

. (Join-Path $PSScriptRoot 'GbMove.Common.ps1')

function Test-IsAdmin {
    $id = [Security.Principal.WindowsIdentity]::GetCurrent()
    return ([Security.Principal.WindowsPrincipal]$id).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}

if (-not $RestoreTestOnly -and -not (Test-IsAdmin)) {
    # Windows features, winget installs, firewall and power settings all need
    # admin. Relaunch elevated (the UAC prompt) and let this window close.
    $relaunch = @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', "`"$PSCommandPath`"", '-InstallDir', "`"$InstallDir`"")
    Start-Process -FilePath 'powershell.exe' -Verb RunAs -ArgumentList $relaunch
    exit 0
}

$logPath = Start-GbLog 'install'
$staging = $null
$restartNeeded = $false
$failed = $false

function Request-Restart([string]$Why) {
    Write-Host ''
    Write-Host 'RESTART NEEDED' -ForegroundColor Yellow
    Write-Note $Why
    Write-Note 'Restart the PC, sign in again, then double-click Install.cmd again.'
    Write-Note 'It continues where it stopped.'
}

function Expand-MovePayload([string]$Into) {
    $payload = Join-Path $PSScriptRoot 'payload.gbmove'
    if (-not (Test-Path -LiteralPath $payload)) { throw "payload.gbmove is not next to Install.ps1 ($PSScriptRoot). Run Install.cmd from the move folder." }
    New-Item -ItemType Directory -Force -Path $Into | Out-Null
    $zip = Join-Path $Into 'payload.zip'
    for ($attempt = 1; $attempt -le 3; $attempt++) {
        $pw = Read-ExistingPassword
        try {
            Unprotect-GbPayload $payload $zip $pw
            break
        } catch {
            if ($attempt -eq 3) { throw }
            Write-Attention "$($_.Exception.Message) Try again."
        }
    }
    $dir = Join-Path $Into 'payload'
    Expand-Archive -LiteralPath $zip -DestinationPath $dir -Force
    Remove-Item -LiteralPath $zip -Force
    return $dir
}

function Assert-CountsMatch($Manifest, $Actual) {
    # A real export stops the app before counting and dumping, so the counts
    # are exact. A -TestOnly export counts while the app keeps writing, so
    # differences there are reported, not fatal - only a missing table is.
    $exact = -not $Manifest.test_only
    $bad = @()
    foreach ($name in $Manifest.counts.PSObject.Properties.Name) {
        $want = [long]$Manifest.counts.$name
        $got = [long]$Actual[$name]
        if ($want -ne $got -and ($exact -or $got -lt 0)) { $bad += "$name (expected $want, got $got)" }
        elseif ($want -ne $got) { Write-Note "$name`: $got rows (old PC had $want when counted; it was still running)" }
    }
    if ($bad.Count) { throw "Restored database does not match the old PC: $($bad -join ', ')" }
}

function Restore-Dump($Target, [string]$DumpPath, [string]$User, [string]$Db) {
    if ($Target.Kind -eq 'compose') {
        Invoke-Native -Exe 'docker' -Arguments @('compose', 'cp', $DumpPath, 'db:/tmp/gb.dump') | Out-Null
    } else {
        Invoke-Native -Exe 'docker' -Arguments @('cp', $DumpPath, "$($Target.Name):/tmp/gb.dump") | Out-Null
    }
    $prefix = Get-DbExecPrefix $Target
    Invoke-Native -Exe 'docker' -Arguments ($prefix + @('pg_restore', '-U', $User, '-d', $Db, '--no-owner', '--exit-on-error', '/tmp/gb.dump')) | Out-Null
    Invoke-Native -Exe 'docker' -Arguments ($prefix + @('rm', '-f', '/tmp/gb.dump')) -AllowFail | Out-Null
}

function Invoke-RestoreTest($PayloadDir, $Manifest) {
    Write-Step 'Restoring the database copy into a throwaway container'
    Wait-DockerEngine
    $name = 'gbmove-restoretest'
    Invoke-Native -Exe 'docker' -Arguments @('rm', '-f', $name) -Quiet -AllowFail | Out-Null
    $user = $Manifest.postgres_user
    $db = $Manifest.postgres_db
    Invoke-Native -Exe 'docker' -Arguments @('run', '-d', '--name', $name, '-e', "POSTGRES_USER=$user", '-e', 'POSTGRES_PASSWORD=restoretest', '-e', "POSTGRES_DB=$db", 'postgres:16-alpine') -Quiet | Out-Null
    try {
        $target = New-ContainerDbTarget $name
        Wait-Postgres $target $user $db
        Restore-Dump $target (Join-Path $PayloadDir 'gb.dump') $user $db
        $alembic = Invoke-Psql $target $user $db 'select version_num from alembic_version'
        if ($alembic -ne $Manifest.alembic_version) { throw "Migration level differs: expected $($Manifest.alembic_version), got $alembic" }
        Assert-CountsMatch $Manifest (Get-TableCounts $target $user $db)
        Write-Ok 'Restore test passed: every key table matches the old PC'
    } finally {
        Invoke-Native -Exe 'docker' -Arguments @('rm', '-f', $name) -Quiet -AllowFail | Out-Null
    }
}

function Install-WingetPackage([string]$Id, [string]$Label, [string[]]$Extra = @()) {
    Write-Note "Installing $Label (a few minutes)..."
    $wingetArgs = @('install', '-e', '--id', $Id, '--accept-package-agreements', '--accept-source-agreements') + $Extra
    Invoke-Native -Exe 'winget' -Arguments $wingetArgs -AllowFail | Out-Null
    Update-SessionPath
}

function New-Shortcut([string]$Path, [string]$Target, [string]$Arguments, [string]$WorkDir, [int]$WindowStyle = 1) {
    $shell = New-Object -ComObject WScript.Shell
    $lnk = $shell.CreateShortcut($Path)
    $lnk.TargetPath = $Target
    $lnk.Arguments = $Arguments
    $lnk.WorkingDirectory = $WorkDir
    $lnk.WindowStyle = $WindowStyle
    $lnk.IconLocation = "$env:SystemRoot\System32\imageres.dll,109"
    $lnk.Save()
}

try {
    Write-Host 'GB Automations - install' -ForegroundColor White
    if ($RestoreTestOnly) { Write-Attention 'TEST MODE: only checks the move file restores correctly. Installs nothing.' }

    Write-Step 'Opening the move file'
    $staging = Join-Path $env:TEMP "gb-move-install-$(Get-Date -Format 'yyyyMMdd-HHmmss')"
    $payloadDir = Expand-MovePayload $staging
    $manifest = Get-Content -LiteralPath (Join-Path $payloadDir 'manifest.json') -Raw | ConvertFrom-Json
    Write-Ok "Move file from $($manifest.source_computer), made $($manifest.created_utc.Substring(0, 16).Replace('T', ' ')) UTC"
    if ($manifest.test_only -and -not $RestoreTestOnly) {
        Write-Attention 'This move file was made in TEST mode - the old PC is still running.'
        if (-not (Confirm-YesNo 'Install anyway? (Only if the old PC is really switched off)')) { throw 'Stopped.' }
    }

    if ($RestoreTestOnly) {
        Invoke-RestoreTest $payloadDir $manifest
        Write-Host ''
        Write-Host 'TEST DONE - the move file is good.' -ForegroundColor Green
        return
    }

    $envMap = Read-DotEnv (Join-Path $payloadDir 'env')

    # ------------------------------------------------------------ Windows
    Write-Step 'Checking Windows'
    $cs = Get-CimInstance Win32_ComputerSystem
    $cpu = Get-CimInstance Win32_Processor | Select-Object -First 1
    if (-not $cs.HypervisorPresent -and -not $cpu.VirtualizationFirmwareEnabled) {
        Write-Attention 'Virtualization looks switched off in this PC''s BIOS/UEFI. Docker needs it.'
        Write-Note 'If Docker fails to start later: enable "Intel VT-x" / "AMD-V" (SVM) in the BIOS.'
    }
    if (-not (Get-Command winget -ErrorAction SilentlyContinue)) {
        throw 'winget is missing. Install "App Installer" from the Microsoft Store, then run Install.cmd again.'
    }
    $explorerOwner = $null
    try {
        $explorer = Get-CimInstance Win32_Process -Filter "Name = 'explorer.exe'" | Select-Object -First 1
        if ($explorer) { $explorerOwner = (Invoke-CimMethod -InputObject $explorer -MethodName GetOwner).User }
    } catch { }
    if ($explorerOwner -and $explorerOwner -ne $env:USERNAME) {
        # Ollama, the desktop icon and the sign-in autostart are per-user: they
        # must land in the account that will actually be signed in.
        Write-Attention "Windows is signed in as '$explorerOwner' but the installer runs as '$env:USERNAME'."
        Write-Note 'Best: sign in with the account that will run GB Automations and make it an administrator.'
        if (-not (Confirm-YesNo "Install for '$env:USERNAME' anyway?")) { throw 'Stopped.' }
    }
    Write-Ok "Windows account: $env:USERNAME"

    foreach ($feature in @('Microsoft-Windows-Subsystem-Linux', 'VirtualMachinePlatform')) {
        $state = (Get-WindowsOptionalFeature -Online -FeatureName $feature).State
        if ($state -eq 'Enabled') { continue }
        if ($state -ne 'EnablePending') {
            Write-Note "Turning on Windows feature $feature..."
            Enable-WindowsOptionalFeature -Online -FeatureName $feature -All -NoRestart | Out-Null
        }
        $restartNeeded = $true
    }
    if (-not $restartNeeded) { Write-Ok 'WSL2 features are on' }

    # ---------------------------------------------------------- programs
    Write-Step 'Installing programs'
    Update-SessionPath
    if (Get-Command git -ErrorAction SilentlyContinue) { Write-Ok 'Git already installed' }
    else {
        Install-WingetPackage 'Git.Git' 'Git' @('--silent')
        if (-not (Get-Command git -ErrorAction SilentlyContinue)) { throw 'Git did not install. Install it from git-scm.com and run Install.cmd again.' }
    }

    if (Get-DockerDesktopExe) { Write-Ok 'Docker Desktop already installed' }
    else {
        # Docker Desktop's license is free for businesses under 250 employees
        # AND under USD 10M revenue (see docs/move-to-new-pc.md).
        Install-WingetPackage 'Docker.DockerDesktop' 'Docker Desktop' @('--override', 'install --quiet --accept-license --backend=wsl-2')
        if (-not (Get-DockerDesktopExe)) { throw 'Docker Desktop did not install. See the log, or install it from docker.com and run Install.cmd again.' }
        $restartNeeded = $true
    }

    if (Get-OllamaExe) { Write-Ok 'Ollama already installed' }
    else {
        Install-WingetPackage 'Ollama.Ollama' 'Ollama' @('--silent')
        if (-not (Get-OllamaExe)) { throw 'Ollama did not install. Install it from ollama.com and run Install.cmd again.' }
    }

    # docker-users membership only takes effect at the next sign-in, and the
    # Start icon runs WITHOUT admin, so it needs it.
    $groupLine = Get-NativeOutput -Exe 'net' -Arguments @('localgroup', 'docker-users') -AllowFail
    if ($groupLine -and ($groupLine -notmatch "(?m)^(.*\\)?$([regex]::Escape($env:USERNAME))\s*$")) {
        Invoke-Native -Exe 'net' -Arguments @('localgroup', 'docker-users', "$env:USERDOMAIN\$env:USERNAME", '/add') -Quiet -AllowFail | Out-Null
        $restartNeeded = $true
    }

    if ($restartNeeded) {
        Request-Restart 'Windows features or Docker Desktop were just installed.'
        return
    }
    Invoke-Native -Exe 'wsl' -Arguments @('--update') -Quiet -AllowFail | Out-Null

    # ------------------------------------------------------------ Docker
    Write-Step 'Starting Docker'
    Wait-DockerEngine
    Write-Ok 'Docker is running'

    # ------------------------------------------------------------ Ollama
    Write-Step 'Setting up the AI model (Ollama)'
    $ollamaExe = Get-OllamaExe
    # Containers reach Ollama via host.docker.internal, so it must listen
    # beyond localhost. The firewall rule exists BEFORE Ollama first listens,
    # so Windows never shows the "allow access?" prompt (a dismissed prompt
    # becomes a block rule that would beat this allow rule). Scoped to the
    # Docker/WSL address ranges, not the office LAN.
    if ([Environment]::GetEnvironmentVariable('OLLAMA_HOST', 'User') -ne '0.0.0.0:11434') {
        [Environment]::SetEnvironmentVariable('OLLAMA_HOST', '0.0.0.0:11434', 'User')
    }
    $env:OLLAMA_HOST = '0.0.0.0:11434'
    if (-not (Get-NetFirewallRule -DisplayName 'GB Automations - Ollama' -ErrorAction SilentlyContinue)) {
        New-NetFirewallRule -DisplayName 'GB Automations - Ollama' -Direction Inbound -Action Allow -Protocol TCP -LocalPort 11434 `
            -Program $ollamaExe -RemoteAddress @('127.0.0.1', '172.16.0.0/12', '192.168.65.0/24') | Out-Null
    }
    # Restart so the running Ollama picks up OLLAMA_HOST.
    Get-Process -Name 'ollama', 'ollama app' -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue
    if (-not (Start-Ollama -TimeoutSec 60)) { throw 'Ollama did not start. Open "Ollama" from the Start menu and run Install.cmd again.' }
    $model = Get-EnvValue $envMap 'OLLAMA_MODEL' 'llama3.1:8b-instruct-q4_K_M'
    $tags = Invoke-RestMethod -Uri 'http://localhost:11434/api/tags' -UseBasicParsing
    if (@($tags.models | Where-Object { $_.name -eq $model }).Count) { Write-Ok "Model $model already downloaded" }
    else {
        Write-Note "Downloading $model (about 5 GB - the slowest step)..."
        # Straight to the console: ollama draws its progress bar with carriage
        # returns, which looks frozen when piped through PowerShell.
        $pull = Start-Process -FilePath $ollamaExe -ArgumentList @('pull', $model) -NoNewWindow -Wait -PassThru
        if ($pull.ExitCode -ne 0) { throw "Downloading the AI model failed (exit $($pull.ExitCode)). Check the internet connection and run Install.cmd again." }
        Write-Ok "Model $model downloaded"
    }

    # --------------------------------------------------------------- NAS
    if (Test-EnvTrue $envMap 'SYNC_NAS_FOLDERS') {
        Write-Step 'Setting up the NAS (W: drive) connection'
        $device = Get-EnvValue $envMap 'NAS_CIFS_DEVICE'
        if ($device -match '^//([^/]+)/') {
            $nasHost = $Matches[1]
            if (-not (Test-NetConnection -ComputerName $nasHost -Port 445 -InformationLevel Quiet -WarningAction SilentlyContinue)) {
                throw "Can't reach the NAS at $nasHost. This PC must be on the office network (cable or office Wi-Fi). Connect it, then run Install.cmd again."
            }
            Write-Ok "NAS $nasHost reachable"
            # A folder in NAS_CIFS_DEVICE that no longer exists on the share
            # (renamed/deleted) makes the mount fail with "no such file or
            # directory", and with it the WHOLE app. Checked with this Windows
            # user's access, which can differ from NAS_USER's, so warn only.
            $uncPath = $device -replace '/', '\'
            if (-not (Test-Path -LiteralPath $uncPath)) {
                Write-Attention "Can't see the NAS folder $uncPath from this PC."
                Write-Note 'If it was renamed or deleted, the app will not start: recreate it, or point NAS_CIFS_DEVICE in .env at the right folder.'
            }
        }
        # The app container won't even start if the share fails to mount
        # (docs/misc/gotchas.md section 15).
        Install-CifsUtils
        Write-Ok 'NAS helper (cifs-utils) installed'
    }

    # ------------------------------------------------------------- power
    Write-Step 'Keeping the PC awake while plugged in'
    Invoke-Native -Exe 'powercfg' -Arguments @('/change', 'standby-timeout-ac', '0') -Quiet -AllowFail | Out-Null
    Invoke-Native -Exe 'powercfg' -Arguments @('/change', 'hibernate-timeout-ac', '0') -Quiet -AllowFail | Out-Null
    Write-Ok 'Sleep and hibernate switched off on mains power (the screen may still turn off)'

    # -------------------------------------------------------------- code
    Write-Step "Putting the code in $InstallDir"
    $commit = $manifest.commit
    if (Test-Path -LiteralPath (Join-Path $InstallDir '.git')) {
        $have = Get-NativeOutput -Exe 'git' -Arguments @('-c', 'safe.directory=*', '-C', $InstallDir, 'rev-parse', 'HEAD') -AllowFail
        if ($have -ne $commit) { Write-Attention "The folder already has code version $have, the move file has $commit. Keeping the folder as it is." }
        else { Write-Ok 'Code already in place' }
    } else {
        if ((Test-Path -LiteralPath $InstallDir) -and (Get-ChildItem -LiteralPath $InstallDir -Force | Select-Object -First 1)) {
            throw "$InstallDir exists and is not empty. Move it away (or run Install.ps1 -InstallDir <other folder named gb-automations>)."
        }
        New-Item -ItemType Directory -Force -Path (Split-Path $InstallDir) | Out-Null
        # safe.directory=*: this elevated session owns the new folder via the
        # Administrators group, which git can treat as "dubious ownership".
        # Ownership is handed to the user further down.
        Invoke-Native -Exe 'git' -Arguments @('-c', 'safe.directory=*', 'clone', '--quiet', (Join-Path $payloadDir 'repo.bundle'), $InstallDir) | Out-Null
        Invoke-Native -Exe 'git' -Arguments @('-c', 'safe.directory=*', '-C', $InstallDir, 'checkout', '--quiet', '-B', 'main', $commit) | Out-Null
        Invoke-Native -Exe 'git' -Arguments @('-c', 'safe.directory=*', '-C', $InstallDir, 'remote', 'set-url', 'origin', $script:GbRepoUrl) | Out-Null
        Write-Ok "Code version $($commit.Substring(0, 7)) in place (updates: git pull from $script:GbRepoUrl)"
    }

    # ------------------------------------------------------------ config
    Write-Step 'Putting settings and the Google key in place'
    New-Item -ItemType Directory -Force -Path (Join-Path $InstallDir 'secrets') | Out-Null
    $placements = @(
        @{ From = (Join-Path $payloadDir 'env'); To = (Join-Path $InstallDir '.env'); Label = '.env' },
        @{ From = (Join-Path $payloadDir 'gcp-service-account.json'); To = (Join-Path $InstallDir 'secrets\gcp-service-account.json'); Label = 'Google key' }
    )
    foreach ($p in $placements) {
        if (-not (Test-Path -LiteralPath $p.To)) {
            Copy-Item -LiteralPath $p.From -Destination $p.To
            Write-Ok "$($p.Label) placed"
        } elseif ((Get-FileHash -LiteralPath $p.From).Hash -eq (Get-FileHash -LiteralPath $p.To).Hash) {
            Write-Ok "$($p.Label) already in place"
        } else {
            Write-Attention "$($p.Label) already exists and differs from the move file - keeping the existing one."
        }
    }

    # Everything above was written by this elevated session, so Windows made the
    # Administrators group its owner. Hand the folder to the user: otherwise a
    # normal (non-admin) session can't edit .env or `git pull`, and git refuses
    # the repo as "dubious ownership".
    $account = "$env:USERDOMAIN\$env:USERNAME"
    Invoke-Native -Exe 'icacls' -Arguments @($InstallDir, '/setowner', $account, '/T', '/C', '/Q') -Quiet -AllowFail | Out-Null
    Invoke-Native -Exe 'icacls' -Arguments @($InstallDir, '/grant', "${account}:(OI)(CI)F", '/T', '/C', '/Q') -Quiet -AllowFail | Out-Null

    Set-Location -LiteralPath $InstallDir
    $pgUser = $manifest.postgres_user
    $pgDb = $manifest.postgres_db

    # ---------------------------------------------------------- database
    Write-Step 'Restoring the database'
    # The api must NOT start before the restore: its first boot would run the
    # migrations on an empty database and the restore would then collide.
    Invoke-Native -Exe 'docker' -Arguments @('compose', 'up', '-d', 'db') | Out-Null
    $db = New-ComposeDbTarget
    Wait-Postgres $db $pgUser $pgDb
    $tableCount = Invoke-Psql $db $pgUser $pgDb "select count(*) from information_schema.tables where table_schema = 'public'"
    if ([int]$tableCount -gt 0) {
        Write-Attention 'This PC already has a GB Automations database - leaving it untouched (never overwritten).'
    } else {
        Restore-Dump $db (Join-Path $payloadDir 'gb.dump') $pgUser $pgDb
        Assert-CountsMatch $manifest (Get-TableCounts $db $pgUser $pgDb)
        Write-Ok 'Database restored - every key table matches the old PC'
    }

    # --------------------------------------------------------------- app
    Write-Step 'Building and starting the app (first time: a few minutes)'
    $upCode = Invoke-Native -Exe 'docker' -Arguments @('compose', 'up', '-d', '--build') -AllowFail
    $healthy = $false
    $deadline = (Get-Date).AddMinutes(5)
    while ($upCode -eq 0 -and (Get-Date) -lt $deadline) {
        if (Test-AppHealthy) { $healthy = $true; break }
        Start-Sleep -Seconds 5
    }
    if (-not $healthy) {
        Write-Note 'Last lines from the app:'
        Invoke-Native -Exe 'docker' -Arguments @('compose', 'logs', '--tail', '40', 'api') -AllowFail | Out-Null
        throw 'The app did not come up. A "mount" error above = NAS problem: the folder in NAS_CIFS_DEVICE must exist and this PC must be on the office network (docs/misc/gotchas.md section 15). Otherwise see docs/move-to-new-pc.md.'
    }
    Write-Ok 'App is running'

    Write-Step 'Catching up on email labelled while the app was off'
    $rc = Invoke-Native -Exe 'docker' -Arguments @('compose', 'exec', '-T', 'api', 'python', '-m', 'gb_automations.scripts.reconcile') -AllowFail
    if ($rc -eq 0) { Write-Ok 'Caught up (the work queue finishes it in the background)' }
    else { Write-Attention 'Catch-up reported a problem - re-run later: docker compose exec api python -m gb_automations.scripts.reconcile' }

    # --------------------------------------------------------- shortcuts
    Write-Step 'Adding the GB Automations icon'
    $startPs1 = Join-Path $InstallDir 'deploy\windows\Start-GbAutomations.ps1'
    $psExe = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
    New-Shortcut -Path (Join-Path ([Environment]::GetFolderPath('Desktop')) 'GB Automations.lnk') -Target $psExe `
        -Arguments "-NoProfile -ExecutionPolicy Bypass -File `"$startPs1`"" -WorkDir $InstallDir
    # Our own sign-in autostart (Startup folder), instead of relying on Docker
    # Desktop's "start at sign-in" setting: it also starts Ollama, and it runs
    # `docker compose up -d`, so the app comes back even if its containers were
    # removed. Runs minimised and only speaks up if something fails.
    New-Shortcut -Path (Join-Path ([Environment]::GetFolderPath('Startup')) 'GB Automations (autostart).lnk') -Target $psExe `
        -Arguments "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$startPs1`" -AtLogon" -WorkDir $InstallDir -WindowStyle 7
    Write-Ok 'Desktop icon "GB Automations" + start at sign-in'

    Write-Host ''
    Write-Host 'INSTALLED.' -ForegroundColor Green
    Write-Note 'The status page opens now. Every row should turn green within a few minutes.'
    Write-Note ''
    Write-Note 'Left for a human:'
    Write-Note ' - In Notion, open the Automations panel on the Projects, Oppgaver and Korreksjoner'
    Write-Note '   databases and switch back on any automation marked as paused.'
    if ($manifest.sync_frame -and ($null -eq $manifest.frame_token_age_days -or [double]$manifest.frame_token_age_days -gt 12)) {
        Write-Note ' - Frame needs a sign-in (its login expired while the app was off):'
        Write-Note "     cd `"$InstallDir`"; docker compose exec api python -m gb_automations.scripts.frame_oauth_bootstrap"
    } else {
        Write-Note ' - If the Frame row on the status page is red, see "Frame" in docs/move-to-new-pc.md.'
    }
    Write-Note ' - Keep the old PC switched off. Delete the move folder from the USB stick.'
    # explorer.exe opens the URL in the signed-in user's (non-admin) browser.
    Start-Process -FilePath 'explorer.exe' -ArgumentList $script:GbStatusUrl
} catch {
    $failed = $true
    Write-Host ''
    Write-Host "FAILED: $($_.Exception.Message)" -ForegroundColor Red
    Write-Note 'Fix the problem above, then double-click Install.cmd again - it continues where it stopped.'
    Write-Note 'Help: docs/move-to-new-pc.md (Troubleshooting).'
    if ($logPath) { Write-Note "Log: $logPath" }
} finally {
    # The unpacked move file holds every secret in plain text - never leave it.
    if ($staging -and (Test-Path -LiteralPath $staging)) {
        Remove-Item -LiteralPath $staging -Recurse -Force -ErrorAction SilentlyContinue
    }
    Stop-GbLog
    if (-not $NoPause) {
        Write-Host ''
        Read-Host 'Press Enter to close this window' | Out-Null
    }
}
if ($failed) { exit 1 }

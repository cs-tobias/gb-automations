# Shared helpers for Export-ForMove.ps1, Install.ps1 and Start-GbAutomations.ps1.
#
# Windows PowerShell 5.1 rules this file (and its callers) follow on purpose:
#   * ASCII only. PS 5.1 reads a BOM-less .ps1 as the ANSI codepage, so any
#     non-ASCII character silently corrupts the script.
#   * Native commands (docker, git, wsl, winget) run with
#     $ErrorActionPreference = 'Continue' and are judged by $LASTEXITCODE.
#     Under 'Stop', PS 5.1 turns harmless stderr progress lines (docker pull,
#     git clone) into terminating errors.
#   * Secret values are never written to the console or the transcript.
#     Only key names are mentioned.

Set-StrictMode -Version 2
$ErrorActionPreference = 'Stop'
# Windows PowerShell 5.1 can default to TLS 1.0/1.1, which GitHub and the
# installer download sites refuse.
[Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12

$script:GbRepoUrl = 'https://github.com/cs-tobias/gb-automations.git'
$script:GbStatusUrl = 'http://localhost:8000/status'
$script:GbHealthUrl = 'http://localhost:8000/health'
$script:GbDefaultInstallDir = 'C:\Automatisering\gb-automations'

# Tables whose row counts go into the move manifest. These hold the state that
# cannot be rebuilt from Notion/Gmail/Frame/Fiken (see docs/move-to-new-pc.md),
# so a count mismatch after restore is the signal that something went wrong.
$script:GbKeyTables = @(
    'oauth_tokens', 'project_labels', 'sync_tasks', 'email_rows',
    'fiken_invoices', 'fiken_invoice_lines', 'faktura_notion_cache',
    'frame_comments', 'frame_leveranse_folders', 'attachment_blobs'
)

# ---------------------------------------------------------------- output / log

function Write-Step([string]$Text) { Write-Host ''; Write-Host "==> $Text" -ForegroundColor Cyan }
function Write-Ok([string]$Text) { Write-Host "    OK   $Text" -ForegroundColor Green }
function Write-Note([string]$Text) { Write-Host "         $Text" }
function Write-Attention([string]$Text) { Write-Host "    !!   $Text" -ForegroundColor Yellow }

function Start-GbLog([string]$Name) {
    $dir = Join-Path $env:ProgramData 'gb-automations'
    try {
        New-Item -ItemType Directory -Force -Path $dir | Out-Null
        # The sign-in autostart writes one log per sign-in; keep two months.
        Get-ChildItem -LiteralPath $dir -Filter "$Name-*.log" -ErrorAction SilentlyContinue |
            Where-Object { $_.LastWriteTime -lt (Get-Date).AddDays(-60) } |
            Remove-Item -Force -ErrorAction SilentlyContinue
        $path = Join-Path $dir ("{0}-{1}.log" -f $Name, (Get-Date -Format 'yyyyMMdd-HHmmss'))
        Start-Transcript -Path $path -Append | Out-Null
        return $path
    } catch {
        return $null
    }
}

function Stop-GbLog { try { Stop-Transcript | Out-Null } catch { } }

function Confirm-YesNo([string]$Question) {
    $answer = Read-Host "$Question [y/N]"
    return ($answer -match '^(y|yes|j|ja)$')
}

# ------------------------------------------------------------- native commands

function Update-SessionPath {
    # Freshly installed tools (git, docker, ollama) add themselves to PATH in
    # the registry; this session's copy is stale until refreshed.
    $machine = [Environment]::GetEnvironmentVariable('Path', 'Machine')
    $user = [Environment]::GetEnvironmentVariable('Path', 'User')
    $env:Path = (@($machine, $user) | Where-Object { $_ }) -join ';'
}

function Assert-CommandExists([string]$Exe) {
    # Without this, a missing program is a non-terminating error under
    # 'Continue' and $LASTEXITCODE keeps the PREVIOUS command's value - a
    # missing tool could silently look like success.
    if (-not (Get-Command $Exe -ErrorAction SilentlyContinue)) {
        throw "The program '$Exe' was not found on this PC."
    }
}

function Invoke-Native {
    param(
        [Parameter(Mandatory)][string]$Exe,
        [string[]]$Arguments = @(),
        [switch]$Quiet,
        [switch]$AllowFail
    )
    Assert-CommandExists $Exe
    $old = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        if ($Quiet) { & $Exe @Arguments *> $null } else { & $Exe @Arguments | Out-Host }
        $code = $LASTEXITCODE
    } catch {
        # A program that exists but cannot be started throws instead of
        # returning an exit code - e.g. winget on a fresh PC, where its App
        # Installer package isn't registered for this Windows account yet.
        # The message carries PowerShell's "At <file>:<line>" position glued
        # on; only the reason means anything to the person installing.
        $reason = ($_.Exception.Message -replace '(?s)\s*At [A-Za-z]:\\.*$', '')
        if (-not $AllowFail) { throw "Could not start $Exe`: $reason" }
        Write-Attention "Could not start $Exe`: $reason"
        $code = -1
    } finally {
        $ErrorActionPreference = $old
    }
    if ($null -eq $code) { $code = 0 }
    if ($code -ne 0 -and -not $AllowFail) {
        throw "Command failed (exit $code): $Exe $($Arguments -join ' ')"
    }
    return $code
}

function Get-NativeOutput {
    param(
        [Parameter(Mandatory)][string]$Exe,
        [string[]]$Arguments = @(),
        [switch]$AllowFail
    )
    Assert-CommandExists $Exe
    $old = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        $out = & $Exe @Arguments 2> $null
        $code = $LASTEXITCODE
    } catch {
        if (-not $AllowFail) { throw "Could not start $Exe`: $($_.Exception.Message)" }
        return $null
    } finally {
        $ErrorActionPreference = $old
    }
    if ($code -ne 0) {
        if ($AllowFail) { return $null }
        throw "Command failed (exit $code): $Exe $($Arguments -join ' ')"
    }
    return (($out | Out-String).Trim())
}

# -------------------------------------------------------------------- docker

function Get-DockerDesktopExe {
    $candidates = @(
        (Join-Path $env:ProgramFiles 'Docker\Docker\Docker Desktop.exe'),
        (Join-Path $env:LOCALAPPDATA 'Programs\Docker\Docker\Docker Desktop.exe')
    )
    foreach ($c in $candidates) { if (Test-Path -LiteralPath $c) { return $c } }
    return $null
}

function Test-DockerEngine {
    if (-not (Get-Command docker -ErrorAction SilentlyContinue)) { return $false }
    return ((Invoke-Native -Exe 'docker' -Arguments @('info') -Quiet -AllowFail) -eq 0)
}

function Set-DockerDesktopNoWindow {
    # Docker Desktop opens its dashboard window every time it starts unless
    # OpenUIOnStartupDisabled is set (its "Open Docker Dashboard when Docker
    # Desktop starts" setting). The app only needs the engine, so Docker should
    # start straight to the tray. Docker reads this file when it launches, so
    # this runs before every launch. Edited as text, not via ConvertTo-Json:
    # PS 5.1 would rewrite the whole file and could mangle settings it doesn't
    # round-trip. Missing file = Docker has never run here; leave its first run
    # alone. Cosmetic only, so any failure is ignored.
    $path = Join-Path $env:APPDATA 'Docker\settings-store.json'
    try {
        if (-not (Test-Path -LiteralPath $path)) { return }
        $text = [IO.File]::ReadAllText($path)
        $key = '"OpenUIOnStartupDisabled"'
        if ($text -match "$key\s*:\s*true") { return }
        if ($text -match "$key\s*:\s*false") {
            $text = $text -replace "$key\s*:\s*false", "$key`: true"
        } elseif ($text -match '^\s*\{\s*\}\s*$') {
            $text = "{$key`: true}"
        } else {
            $text = ([regex]'\{').Replace($text, "{$key`: true,", 1)
        }
        # Docker's JSON reader rejects a byte-order mark, so no BOM.
        [IO.File]::WriteAllText($path, $text, (New-Object Text.UTF8Encoding $false))
    } catch { }
}

function Wait-DockerEngine([int]$TimeoutSec = 300) {
    Update-SessionPath
    if (Test-DockerEngine) { return }
    $exe = Get-DockerDesktopExe
    if (-not $exe) { throw 'Docker Desktop is not installed on this PC.' }
    # Already launching (e.g. its own start-at-sign-in got there first): just
    # wait. Launching it a second time pops its dashboard window open, and
    # several launches in a row can kill each other mid-start.
    if (Get-Process -Name 'Docker Desktop' -ErrorAction SilentlyContinue) {
        Write-Note 'Docker Desktop is starting, waiting for it...'
    } else {
        Write-Note 'Starting Docker Desktop in the background (this can take a minute or two)...'
        Set-DockerDesktopNoWindow
        # Launched through explorer.exe so it runs as the signed-in user even
        # when the caller (Install.ps1) is elevated. Docker Desktop is meant to
        # run un-elevated, as it does at every sign-in afterwards.
        Start-Process -FilePath 'explorer.exe' -ArgumentList "`"$exe`"" | Out-Null
    }
    $deadline = (Get-Date).AddSeconds($TimeoutSec)
    while ((Get-Date) -lt $deadline) {
        Start-Sleep -Seconds 5
        Update-SessionPath
        if (Test-DockerEngine) { return }
    }
    throw "Docker Desktop did not finish starting within $([int]($TimeoutSec / 60)) minutes. Open Docker Desktop from the Start menu, wait until it says 'Engine running', then run this again."
}

function Test-AppHealthy {
    try {
        $h = Invoke-RestMethod -Uri $script:GbHealthUrl -TimeoutSec 5 -UseBasicParsing
        return ($h.db -eq 'ok')
    } catch {
        return $false
    }
}

# -------------------------------------------------------------------- ollama

function Get-OllamaExe {
    $c = Join-Path $env:LOCALAPPDATA 'Programs\Ollama\ollama.exe'
    if (Test-Path -LiteralPath $c) { return $c }
    $cmd = Get-Command ollama -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Source }
    return $null
}

function Test-OllamaUp {
    try { Invoke-RestMethod -Uri 'http://localhost:11434/api/tags' -TimeoutSec 3 -UseBasicParsing | Out-Null; return $true } catch { return $false }
}

function Start-Ollama([int]$TimeoutSec = 30) {
    if (Test-OllamaUp) { return $true }
    $exe = Get-OllamaExe
    if (-not $exe) { return $false }
    # Prefer the tray app (what Ollama's own installer autostarts); fall back
    # to the bare server if a future Ollama version renames the app.
    $app = Join-Path (Split-Path $exe) 'ollama app.exe'
    if (Test-Path -LiteralPath $app) { Start-Process -FilePath $app | Out-Null }
    else { Start-Process -FilePath $exe -ArgumentList 'serve' -WindowStyle Hidden | Out-Null }
    $deadline = (Get-Date).AddSeconds($TimeoutSec)
    while ((Get-Date) -lt $deadline) {
        Start-Sleep -Seconds 2
        if (Test-OllamaUp) { return $true }
    }
    return $false
}

function Install-CifsUtils {
    # Docker Desktop's VM can't mount the NAS share without cifs-utils, and a
    # Docker Desktop upgrade can replace the VM and drop it again - so both the
    # installer and the start icon make sure it's there.
    if ((Invoke-Native -Exe 'wsl' -Arguments @('-d', 'docker-desktop', 'sh', '-c', 'command -v mount.cifs') -Quiet -AllowFail) -eq 0) { return }
    Invoke-Native -Exe 'wsl' -Arguments @('-d', 'docker-desktop', 'apk', 'add', 'cifs-utils') | Out-Null
}

# A Postgres target is either the compose service 'db' (Kind=compose) or a
# throwaway container (Kind=container). Both are driven through `docker exec`.
function New-ComposeDbTarget { return @{ Kind = 'compose'; Name = 'db' } }
function New-ContainerDbTarget([string]$Name) { return @{ Kind = 'container'; Name = $Name } }

function Get-DbExecPrefix($Target) {
    if ($Target.Kind -eq 'compose') { return @('compose', 'exec', '-T', $Target.Name) }
    return @('exec', $Target.Name)
}

function Get-DbLogs($Target) {
    if ($Target.Kind -eq 'compose') {
        return (Get-NativeOutput -Exe 'docker' -Arguments @('compose', 'logs', '--no-color', $Target.Name) -AllowFail)
    }
    return (Get-NativeOutput -Exe 'docker' -Arguments @('logs', $Target.Name) -AllowFail)
}

function Invoke-Psql($Target, [string]$User, [string]$Db, [string]$Sql, [switch]$AllowFail) {
    $psqlArgs = (Get-DbExecPrefix $Target) + @('psql', '-U', $User, '-d', $Db, '-At', '-v', 'ON_ERROR_STOP=1', '-c', $Sql)
    return (Get-NativeOutput -Exe 'docker' -Arguments $psqlArgs -AllowFail:$AllowFail)
}

function Wait-Postgres($Target, [string]$User, [string]$Db, [int]$TimeoutSec = 180) {
    # The official postgres image runs a temporary, socket-only server while it
    # initialises a fresh volume, then restarts. pg_isready says "ready" during
    # that window too, so wait for the entrypoint's own "init done" or "skipping
    # init" marker first, then for two successful queries in a row.
    $deadline = (Get-Date).AddSeconds($TimeoutSec)
    while ((Get-Date) -lt $deadline) {
        $logs = Get-DbLogs $Target
        if ($logs -and ($logs -match 'init process complete' -or $logs -match 'Skipping initialization')) { break }
        Start-Sleep -Seconds 2
    }
    $okInARow = 0
    while ((Get-Date) -lt $deadline) {
        $r = Invoke-Psql $Target $User $Db 'select 1' -AllowFail
        if ($r -eq '1') { $okInARow++ } else { $okInARow = 0 }
        if ($okInARow -ge 2) { return }
        Start-Sleep -Seconds 2
    }
    throw 'The database did not become ready in time.'
}

function Get-TableCounts($Target, [string]$User, [string]$Db) {
    $counts = [ordered]@{}
    foreach ($t in $script:GbKeyTables) {
        $r = Invoke-Psql $Target $User $Db "select count(*) from $t" -AllowFail
        if ($r -match '^\d+$') { $counts[$t] = [long]$r } else { $counts[$t] = -1 }
    }
    return $counts
}

# --------------------------------------------------------------------- .env

function Read-DotEnv([string]$Path) {
    # Mirrors docker compose's env_file parsing closely enough for the keys
    # these scripts need: last duplicate wins, quotes stripped, " #" starts an
    # inline comment on unquoted values.
    $map = @{}
    foreach ($line in (Get-Content -LiteralPath $Path -Encoding UTF8)) {
        $t = $line.Trim()
        if ($t -eq '' -or $t.StartsWith('#')) { continue }
        $i = $t.IndexOf('=')
        if ($i -lt 1) { continue }
        $k = $t.Substring(0, $i).Trim()
        $v = $t.Substring($i + 1).Trim()
        if ($v.Length -ge 2 -and (($v.StartsWith('"') -and $v.EndsWith('"')) -or ($v.StartsWith("'") -and $v.EndsWith("'")))) {
            $v = $v.Substring(1, $v.Length - 2)
        } else {
            $h = $v.IndexOf(' #')
            if ($h -ge 0) { $v = $v.Substring(0, $h).TrimEnd() }
        }
        $map[$k] = $v
    }
    return $map
}

function Get-EnvValue($Map, [string]$Key, [string]$Default = '') {
    if ($Map.ContainsKey($Key) -and $Map[$Key] -ne '') { return $Map[$Key] }
    return $Default
}

function Test-EnvTrue($Map, [string]$Key) {
    return ((Get-EnvValue $Map $Key 'false') -match '^(1|true|yes|on)$')
}

# ------------------------------------------------------------ move-file crypto
#
# Format of payload.gbmove:
#   "GBMOVE1" | int32 PBKDF2 iterations | 16-byte salt | 16-byte IV |
#   AES-256-CBC ciphertext | 32-byte HMAC-SHA256 over everything before it
# Keys: PBKDF2-SHA256(password, salt) -> 64 bytes = 32 AES key + 32 HMAC key.
# Encrypt-then-MAC, so a wrong password or a damaged USB copy is caught before
# any decryption happens. Plain .NET Framework 4.8 (every Windows 10/11), no
# 7-Zip or other installs needed on either PC.

$script:GbMoveMagic = [Text.Encoding]::ASCII.GetBytes('GBMOVE1')
$script:GbMoveIterations = 200000
$script:GbMoveHeaderLen = 7 + 4 + 16 + 16

function ConvertTo-PlainText([Security.SecureString]$Secure) {
    $bstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($Secure)
    try { return [Runtime.InteropServices.Marshal]::PtrToStringBSTR($bstr) }
    finally { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr) }
}

function New-RandomBytes([int]$Count) {
    $bytes = New-Object byte[] $Count
    $rng = [Security.Cryptography.RandomNumberGenerator]::Create()
    try { $rng.GetBytes($bytes) } finally { $rng.Dispose() }
    return , $bytes
}

function New-GbMoveKeys([Security.SecureString]$Password, [byte[]]$Salt, [int]$Iterations) {
    $pw = [Text.Encoding]::UTF8.GetBytes((ConvertTo-PlainText $Password))
    $kdf = [Security.Cryptography.Rfc2898DeriveBytes]::new($pw, $Salt, $Iterations, [Security.Cryptography.HashAlgorithmName]::SHA256)
    try { $material = $kdf.GetBytes(64) } finally { $kdf.Dispose(); [Array]::Clear($pw, 0, $pw.Length) }
    $enc = New-Object byte[] 32
    $mac = New-Object byte[] 32
    [Array]::Copy($material, 0, $enc, 0, 32)
    [Array]::Copy($material, 32, $mac, 0, 32)
    return @{ Enc = $enc; Mac = $mac }
}

function New-GbAes([byte[]]$Key, [byte[]]$IV) {
    $aes = [Security.Cryptography.Aes]::Create()
    $aes.Mode = [Security.Cryptography.CipherMode]::CBC
    $aes.Padding = [Security.Cryptography.PaddingMode]::PKCS7
    $aes.Key = $Key
    $aes.IV = $IV
    return $aes
}

function Protect-GbPayload([string]$InFile, [string]$OutFile, [Security.SecureString]$Password) {
    $salt = New-RandomBytes 16
    $iv = New-RandomBytes 16
    $keys = New-GbMoveKeys $Password $salt $script:GbMoveIterations
    $aes = New-GbAes $keys.Enc $iv
    $out = [IO.File]::Open($OutFile, [IO.FileMode]::Create, [IO.FileAccess]::ReadWrite, [IO.FileShare]::None)
    try {
        $iter = [BitConverter]::GetBytes([int32]$script:GbMoveIterations)
        foreach ($part in @($script:GbMoveMagic, $iter, $salt, $iv)) { $out.Write($part, 0, $part.Length) }
        $in = [IO.File]::OpenRead($InFile)
        try {
            $cs = [Security.Cryptography.CryptoStream]::new($out, $aes.CreateEncryptor(), [Security.Cryptography.CryptoStreamMode]::Write, $true)
            try { $in.CopyTo($cs); $cs.FlushFinalBlock() } finally { $cs.Dispose() }
        } finally { $in.Dispose() }
        $out.Position = 0
        $hmac = [Security.Cryptography.HMACSHA256]::new($keys.Mac)
        try { $tag = $hmac.ComputeHash($out) } finally { $hmac.Dispose() }
        $out.Write($tag, 0, $tag.Length)
    } finally {
        $out.Dispose()
        $aes.Dispose()
    }
}

function Unprotect-GbPayload([string]$InFile, [string]$OutFile, [Security.SecureString]$Password) {
    $in = [IO.File]::OpenRead($InFile)
    try {
        $len = $in.Length
        if ($len -lt ($script:GbMoveHeaderLen + 16 + 32)) { throw 'The move file is too small - it is damaged or incomplete.' }
        $header = New-Object byte[] $script:GbMoveHeaderLen
        [void]$in.Read($header, 0, $header.Length)
        for ($i = 0; $i -lt 7; $i++) {
            if ($header[$i] -ne $script:GbMoveMagic[$i]) { throw 'This is not a GB Automations move file.' }
        }
        $iterations = [BitConverter]::ToInt32($header, 7)
        $salt = New-Object byte[] 16
        $iv = New-Object byte[] 16
        [Array]::Copy($header, 11, $salt, 0, 16)
        [Array]::Copy($header, 27, $iv, 0, 16)
        $keys = New-GbMoveKeys $Password $salt $iterations

        $buf = New-Object byte[] 1048576
        $hmac = [Security.Cryptography.HMACSHA256]::new($keys.Mac)
        try {
            $in.Position = 0
            $remaining = $len - 32
            while ($remaining -gt 0) {
                $n = $in.Read($buf, 0, [int][Math]::Min([long]$buf.Length, $remaining))
                if ($n -le 0) { throw 'Unexpected end of the move file.' }
                [void]$hmac.TransformBlock($buf, 0, $n, $null, 0)
                $remaining -= $n
            }
            [void]$hmac.TransformFinalBlock((New-Object byte[] 0), 0, 0)
            $expected = $hmac.Hash
        } finally { $hmac.Dispose() }
        $tag = New-Object byte[] 32
        [void]$in.Read($tag, 0, 32)
        $diff = 0
        for ($i = 0; $i -lt 32; $i++) { $diff = $diff -bor ($tag[$i] -bxor $expected[$i]) }
        if ($diff -ne 0) { throw 'Wrong password, or the move file is damaged.' }

        $aes = New-GbAes $keys.Enc $iv
        $out = [IO.File]::Create($OutFile)
        try {
            $cs = [Security.Cryptography.CryptoStream]::new($out, $aes.CreateDecryptor(), [Security.Cryptography.CryptoStreamMode]::Write, $true)
            try {
                $in.Position = $script:GbMoveHeaderLen
                $remaining = $len - 32 - $script:GbMoveHeaderLen
                while ($remaining -gt 0) {
                    $n = $in.Read($buf, 0, [int][Math]::Min([long]$buf.Length, $remaining))
                    if ($n -le 0) { throw 'Unexpected end of the move file.' }
                    $cs.Write($buf, 0, $n)
                    $remaining -= $n
                }
                $cs.FlushFinalBlock()
            } finally { $cs.Dispose() }
        } finally {
            $out.Dispose()
            $aes.Dispose()
        }
    } finally {
        $in.Dispose()
    }
}

# GBMOVE_PASSWORD lets the export/restore TEST modes run without a person at
# the keyboard (used to verify the scripts). Normal use types the password.
function Get-PasswordFromEnv {
    if ($env:GBMOVE_PASSWORD) { return (ConvertTo-SecureString $env:GBMOVE_PASSWORD -AsPlainText -Force) }
    return $null
}

function Read-ExistingPassword {
    $fromEnv = Get-PasswordFromEnv
    if ($fromEnv) { return $fromEnv }
    return (Read-Host -AsSecureString 'Password for the move file')
}

function Read-NewPassword {
    $fromEnv = Get-PasswordFromEnv
    if ($fromEnv) { return $fromEnv }
    while ($true) {
        $a = Read-Host -AsSecureString 'Choose a password for the move file (at least 8 characters)'
        $b = Read-Host -AsSecureString 'Type the same password again'
        $pa = ConvertTo-PlainText $a
        $pb = ConvertTo-PlainText $b
        if ($pa.Length -lt 8) { Write-Attention 'Too short - use at least 8 characters.'; continue }
        if ($pa -cne $pb) { Write-Attention 'The two passwords were different. Try again.'; continue }
        return $a
    }
}

# "Check for updates" in the GB Automations program. Also works from a
# terminal: powershell -File deploy\windows\Update-GbAutomations.ps1
#
# Updates come from `origin` (Install points it at the cs-tobias GitHub repo),
# branch main, and only ever fast-forward: this PC never has code of its own.
#
#   -CheckOnly   look, change nothing.
#                Exit 0 = up to date, 10 = an update is waiting, 1 = error.
#   (default)    install the update.
#                Exit 0 = installed (or nothing to do), 1 = not installed,
#                3 = the new version didn't start, so the old one was put
#                back, 4 = the new version didn't start and the old one
#                couldn't be put back automatically.
#
# Lines starting with GB- are read by the program:
#   GB-CHANGE: <text>      one line per change in the waiting update
#   GB-RESTART-PROGRAM     the program itself changed; it should reopen

[CmdletBinding()]
param([switch]$CheckOnly)

. (Join-Path $PSScriptRoot 'GbMove.Common.ps1')

$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
# A repo that suddenly wants a password must fail, not wait for typing that
# can never come (the program has no console).
$env:GIT_TERMINAL_PROMPT = '0'

function Get-Git([string[]]$Arguments) { return (Get-NativeOutput -Exe 'git' -Arguments $Arguments) }

function Get-Version([string]$Rev) {
    return (Get-Git @('log', '-1', '--format=%h (%cd)', '--date=format:%d %b %Y', $Rev))
}

function Invoke-Update {
    Set-Location -LiteralPath $repoRoot

    Write-Step 'Checking for updates'
    if ((Invoke-Native -Exe 'git' -Arguments @('fetch', '--quiet', 'origin', 'main') -AllowFail) -ne 0) {
        throw 'Could not reach GitHub to check for updates. Is this PC online?'
    }
    $current = Get-Git @('rev-parse', 'HEAD')
    $latest = Get-Git @('rev-parse', 'FETCH_HEAD')
    if ($current -eq $latest) {
        Write-Ok "Up to date: version $(Get-Version $current)"
        return 0
    }
    if ((Invoke-Native -Exe 'git' -Arguments @('merge-base', '--is-ancestor', $current, $latest) -Quiet -AllowFail) -ne 0) {
        throw 'This PC has code that is not on GitHub, so it cannot be updated automatically. Ask the developer.'
    }

    $changes = @((Get-Git @('log', '--reverse', '--format=%s', "$current..$latest")) -split "`r?`n" | Where-Object { $_ })
    $files = @((Get-Git @('diff', '--name-only', $current, $latest)) -split "`r?`n" | Where-Object { $_ })
    # Docs and the Windows scripts are read straight from disk; anything else
    # (code, migrations, Dockerfile, compose, prompts) is baked into the app's
    # image and needs a rebuild.
    $needsRebuild = @($files | Where-Object { $_ -notmatch '^(deploy/windows/|docs/)' -and $_ -notmatch '\.md$' -and $_ -ne '.gitattributes' }).Count -gt 0
    $hasMigration = @($files | Where-Object { $_ -like 'migrations/*' }).Count -gt 0
    $programChanged = @($files | Where-Object { $_ -like 'deploy/windows/*' }).Count -gt 0

    Write-Ok "Update available: version $(Get-Version $latest), $($changes.Count) change(s)"
    foreach ($c in $changes) { Write-Host "GB-CHANGE: $c" }
    if ($needsRebuild) { Write-Note 'Installing it restarts the app, which is offline for about a minute.' }
    if ($CheckOnly) { return 10 }

    $dirty = Get-Git @('status', '--porcelain', '--untracked-files=no')
    if ($dirty) {
        Write-Attention 'Files in the program folder were changed on this PC:'
        $dirty -split "`r?`n" | ForEach-Object { Write-Note $_ }
        throw 'The update was not installed, so those changes are kept. Ask the developer.'
    }

    Write-Step 'Installing the update'
    Invoke-Native -Exe 'git' -Arguments @('merge', '--ff-only', '--quiet', $latest) | Out-Null
    Write-Ok "Code is now version $(Get-Version $latest)"

    if ($needsRebuild) {
        Write-Step 'Rebuilding and restarting the app (a few minutes)'
        Wait-DockerEngine
        $built = (Invoke-Native -Exe 'docker' -Arguments @('compose', 'up', '-d', '--build') -AllowFail) -eq 0
        if ($built -and (Wait-AppHealthy 240)) {
            Write-Ok 'The app is running the new version'
        } elseif ($hasMigration) {
            # The new version already changed the database's structure, and
            # old code refuses to start against a newer database (alembic
            # can't find the revision). Going back would make things worse.
            Write-Attention 'The new version is not answering, and it changed the database, so going back automatically is not safe.'
            return 4
        } else {
            Write-Attention 'The new version is not answering. Putting the previous version back...'
            Invoke-Native -Exe 'git' -Arguments @('reset', '--hard', '--quiet', $current) | Out-Null
            $rebuilt = (Invoke-Native -Exe 'docker' -Arguments @('compose', 'up', '-d', '--build') -AllowFail) -eq 0
            if ($rebuilt -and (Wait-AppHealthy 240)) {
                Write-Ok "Back on version $(Get-Version $current). It is running."
                return 3
            }
            Write-Attention 'The previous version is not answering either.'
            return 4
        }
    } else {
        Write-Ok 'Nothing the app runs on changed, so it keeps running untouched'
    }
    if ($programChanged) { Write-Host 'GB-RESTART-PROGRAM' }
    return 0
}

$logPath = Start-GbLog 'update'
$code = 1
try {
    $code = Invoke-Update
} catch {
    Write-Host ''
    Write-Host "PROBLEM: $($_.Exception.Message)" -ForegroundColor Red
    if ($logPath) { Write-Note "Log: $logPath" }
    $code = 1
} finally {
    Stop-GbLog
}
exit $code

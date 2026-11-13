# GB Automations - the program. The desktop icon and the sign-in autostart
# open this window:
#   - a status light (Starting / Running / Not running / Updating), from /health
#   - the version this PC runs, and "Check for updates" (Update-GbAutomations)
#   - the app's live log (docker compose logs --follow api)
# Opening it also starts whatever isn't running (Start-GbAutomations.ps1
# -NoLiveLog) and keeps the desktop icon + sign-in autostart pointing here.
# Closing it never stops the app.
#
# Windows PowerShell 5.1 + WinForms, so a fresh PC needs nothing extra. Every
# slow job (starting, updating, the log) runs as a child process; a timer on
# the UI thread reads their output, so the window never freezes.
#
# -Preview [-ScreenshotPath x.png]: layout check with sample lines. Starts,
#   changes and runs nothing - for checking the look on the dev PC, which
#   must never start the app (two PCs at once, docs/move-to-new-pc.md).

[CmdletBinding()]
param([switch]$Preview, [string]$ScreenshotPath)

Add-Type -AssemblyName System.Windows.Forms, System.Drawing
# There's no console to read an error from, so say it in a message box.
trap {
    [System.Windows.Forms.MessageBox]::Show("GB Automations could not open:`n`n$($_.Exception.Message)`n`nSee Troubleshooting in docs\move-to-new-pc.md.", 'GB Automations', 'OK', 'Error') | Out-Null
    exit 1
}
. (Join-Path $PSScriptRoot 'GbMove.Common.ps1')

$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$psExe = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
$programTitle = 'GB Automations'

# One window at a time: a second double-click brings the open one forward.
$mutex = $null
if (-not $Preview) {
    $createdNew = $false
    $mutex = [Threading.Mutex]::new($true, 'Local\GbAutomationsProgram', [ref]$createdNew)
    if (-not $createdNew) {
        try { (New-Object -ComObject WScript.Shell).AppActivate($programTitle) | Out-Null } catch { }
        exit 0
    }
}

Add-Type -ReferencedAssemblies System.Windows.Forms -TypeDefinition @'
using System;
using System.Collections.Concurrent;
using System.Diagnostics;
using System.IO;
using System.Net;
using System.Runtime.InteropServices;
using System.Text;
using System.Threading;

namespace GbAutomations
{
    // A child process whose output lines are queued for the UI timer. PowerShell
    // event handlers can't run on the reader threads, hence the queue.
    public class Child
    {
        private readonly ConcurrentQueue<string> lines = new ConcurrentQueue<string>();
        private readonly Process proc;
        public readonly DateTime StartedUtc = DateTime.UtcNow;

        public Child(string exe, string args, string workDir)
        {
            ProcessStartInfo psi = new ProcessStartInfo(exe, args);
            psi.UseShellExecute = false;
            psi.CreateNoWindow = true;
            psi.RedirectStandardOutput = true;
            psi.RedirectStandardError = true;
            psi.StandardOutputEncoding = new UTF8Encoding(false);
            psi.StandardErrorEncoding = new UTF8Encoding(false);
            psi.WorkingDirectory = workDir;
            psi.EnvironmentVariables["GIT_TERMINAL_PROMPT"] = "0";
            proc = new Process();
            proc.StartInfo = psi;
            proc.OutputDataReceived += delegate (object s, DataReceivedEventArgs e) { if (e.Data != null) lines.Enqueue(e.Data); };
            proc.ErrorDataReceived += delegate (object s, DataReceivedEventArgs e) { if (e.Data != null) lines.Enqueue(e.Data); };
            proc.Start();
            proc.BeginOutputReadLine();
            proc.BeginErrorReadLine();
        }

        public bool TryTake(out string line) { return lines.TryDequeue(out line); }

        public bool HasExited { get { return proc.HasExited; } }

        // Exited AND every line it wrote has been taken.
        public bool Finished
        {
            get
            {
                if (!proc.HasExited) return false;
                proc.WaitForExit();
                return lines.IsEmpty;
            }
        }

        public int ExitCode { get { return proc.ExitCode; } }

        public double Seconds { get { return (DateTime.UtcNow - StartedUtc).TotalSeconds; } }

        public void Discard() { string ignored; while (lines.TryDequeue(out ignored)) { } }

        public void Stop()
        {
            try
            {
                if (proc.HasExited) return;
                // The whole tree: `docker compose` runs docker-compose.exe as a child.
                ProcessStartInfo k = new ProcessStartInfo("taskkill.exe", "/T /F /PID " + proc.Id);
                k.UseShellExecute = false;
                k.CreateNoWindow = true;
                Process kp = Process.Start(k);
                if (kp != null) kp.WaitForExit(5000);
            }
            catch { }
        }
    }

    // Polls /health on a background thread: -1 not known yet, 0 down, 1 up.
    public class HealthProbe
    {
        private volatile int state = -1;
        public int State { get { return state; } }

        public HealthProbe(string url)
        {
            Thread t = new Thread(delegate ()
            {
                while (true)
                {
                    try
                    {
                        HttpWebRequest req = (HttpWebRequest)WebRequest.Create(url);
                        req.Timeout = 3000;
                        req.ReadWriteTimeout = 3000;
                        req.Proxy = null;
                        using (HttpWebResponse resp = (HttpWebResponse)req.GetResponse())
                        using (StreamReader r = new StreamReader(resp.GetResponseStream()))
                        {
                            string body = r.ReadToEnd().Replace(" ", "");
                            state = body.Contains("\"db\":\"ok\"") ? 1 : 0;
                        }
                    }
                    catch { state = 0; }
                    Thread.Sleep(4000);
                }
            });
            t.IsBackground = true;
            t.Start();
        }
    }

    public static class Native
    {
        [DllImport("user32.dll")] public static extern bool SetProcessDPIAware();
        [DllImport("kernel32.dll")] private static extern IntPtr GetConsoleWindow();
        [DllImport("user32.dll")] private static extern bool ShowWindow(IntPtr hWnd, int nCmdShow);
        // Windows applies the launcher's "start hidden/minimised" to the FIRST
        // ShowWindow call of a process. Spend it on PowerShell's console, so the
        // program window itself always opens normally - however it was started.
        public static void HideConsole() { IntPtr h = GetConsoleWindow(); if (h != IntPtr.Zero) ShowWindow(h, 0); }
        [DllImport("user32.dll")] private static extern IntPtr SendMessage(IntPtr hWnd, int msg, IntPtr wParam, IntPtr lParam);
        public static void SetRedraw(IntPtr hWnd, bool on) { SendMessage(hWnd, 0x000B, on ? new IntPtr(1) : IntPtr.Zero, IntPtr.Zero); }
    }
}
'@

[GbAutomations.Native]::HideConsole()
# Crisp text on scaled (125%/150%) screens instead of a blurry stretch.
[GbAutomations.Native]::SetProcessDPIAware() | Out-Null
[System.Windows.Forms.Application]::EnableVisualStyles()

# ------------------------------------------------------------------ look
# Non-ASCII characters are built from code points: this file must stay ASCII
# (see GbMove.Common.ps1).
$dot = [string][char]0x25CF
$bullet = [string][char]0x2022
$ellipsis = [string][char]0x2026
$middot = [string][char]0x00B7

function Get-Rgb([int]$r, [int]$g, [int]$b) { return [Drawing.Color]::FromArgb($r, $g, $b) }
$ui = @{
    Back      = Get-Rgb 246 247 249
    Text      = Get-Rgb 31 35 40
    Muted     = Get-Rgb 101 109 118
    Green     = Get-Rgb 26 127 55
    Amber     = Get-Rgb 154 103 0
    Red       = Get-Rgb 207 34 46
    Gray      = Get-Rgb 140 149 159
    Primary   = Get-Rgb 31 111 235
    Border    = Get-Rgb 208 215 222
    LogBack   = Get-Rgb 30 30 30
}
$logColors = @{
    Default = Get-Rgb 212 212 212
    Program = Get-Rgb 171 178 191
    Step    = Get-Rgb 97 175 239
    Ok      = Get-Rgb 35 209 139
    Warn    = Get-Rgb 229 192 123
    Error   = Get-Rgb 241 76 76
    Dim     = Get-Rgb 128 128 128
}
# ANSI SGR foreground codes the app's ColorFormatter uses (src/gb_automations/obs.py).
$ansi = @{
    '30' = Get-Rgb 128 128 128; '31' = $logColors.Error; '32' = $logColors.Ok; '33' = $logColors.Warn
    '34' = Get-Rgb 97 175 239;  '35' = Get-Rgb 198 120 221; '36' = Get-Rgb 86 182 194; '37' = Get-Rgb 220 223 228
    '90' = Get-Rgb 128 128 128; '91' = Get-Rgb 255 123 114; '92' = Get-Rgb 126 231 135; '93' = Get-Rgb 255 214 102
    '94' = Get-Rgb 130 190 255; '95' = Get-Rgb 219 148 240; '96' = Get-Rgb 118 227 234; '97' = Get-Rgb 255 255 255
    '41' = $logColors.Error
}
$ansiPattern = New-Object Text.RegularExpressions.Regex (([string][char]27) + '\[([0-9;]*)m')
# The app's log uses emoji as markers. This text box can't draw them (they'd
# show as empty boxes), and the level colours carry the meaning anyway.
$emojiPattern = New-Object Text.RegularExpressions.Regex ('(?:\p{Cs}\p{Cs}|\p{So})[' + [char]0xFE0F + [char]0x200D + ']* ?')

$screen = [Drawing.Graphics]::FromHwnd([IntPtr]::Zero)
$scale = $screen.DpiX / 96.0
$screen.Dispose()
function ScalePx([double]$px) { return [int][Math]::Round($px * $scale) }

$fontUi = New-Object Drawing.Font 'Segoe UI', 10
$fontStatus = New-Object Drawing.Font 'Segoe UI Semibold', 15
$fontSmall = New-Object Drawing.Font 'Segoe UI', 9
$fontLog = New-Object Drawing.Font 'Consolas', 9.75

# ---------------------------------------------------------------- window

$form = New-Object Windows.Forms.Form
$form.Text = $programTitle
$form.BackColor = $ui.Back
$form.ForeColor = $ui.Text
$form.Font = $fontUi
$form.StartPosition = 'CenterScreen'
$form.ClientSize = New-Object Drawing.Size (ScalePx 1000), (ScalePx 640)
$form.MinimumSize = New-Object Drawing.Size (ScalePx 640), (ScalePx 420)
$iconPath = Join-Path $PSScriptRoot 'gb-automations.ico'
if (Test-Path -LiteralPath $iconPath) { try { $form.Icon = New-Object Drawing.Icon $iconPath } catch { } }

$root = New-Object Windows.Forms.TableLayoutPanel
$root.Dock = 'Fill'
$root.ColumnCount = 1
$root.RowCount = 3
$root.Padding = New-Object Windows.Forms.Padding (ScalePx 18), (ScalePx 14), (ScalePx 18), (ScalePx 10)
[void]$root.RowStyles.Add((New-Object Windows.Forms.RowStyle 'AutoSize'))
[void]$root.RowStyles.Add((New-Object Windows.Forms.RowStyle 'Percent', 100))
[void]$root.RowStyles.Add((New-Object Windows.Forms.RowStyle 'AutoSize'))
$form.Controls.Add($root)

# Header: status + version on the left, buttons on the right.
$header = New-Object Windows.Forms.TableLayoutPanel
$header.Dock = 'Fill'
$header.AutoSize = $true
$header.ColumnCount = 2
$header.RowCount = 1
[void]$header.ColumnStyles.Add((New-Object Windows.Forms.ColumnStyle 'Percent', 100))
[void]$header.ColumnStyles.Add((New-Object Windows.Forms.ColumnStyle 'AutoSize'))
$header.Margin = New-Object Windows.Forms.Padding 0, 0, 0, (ScalePx 12)

$left = New-Object Windows.Forms.FlowLayoutPanel
$left.FlowDirection = 'TopDown'
$left.AutoSize = $true
$left.WrapContents = $false
$left.Margin = New-Object Windows.Forms.Padding 0

$statusLabel = New-Object Windows.Forms.Label
$statusLabel.AutoSize = $true
$statusLabel.Font = $fontStatus
$statusLabel.Margin = New-Object Windows.Forms.Padding 0
$statusLabel.Text = "$dot  Starting$ellipsis"
$statusLabel.ForeColor = $ui.Amber

$versionLabel = New-Object Windows.Forms.Label
$versionLabel.AutoSize = $true
$versionLabel.Font = $fontSmall
$versionLabel.ForeColor = $ui.Muted
$versionLabel.Margin = New-Object Windows.Forms.Padding (ScalePx 3), (ScalePx 2), 0, 0

$left.Controls.Add($statusLabel)
$left.Controls.Add($versionLabel)

$buttons = New-Object Windows.Forms.FlowLayoutPanel
$buttons.AutoSize = $true
$buttons.WrapContents = $false
$buttons.Anchor = 'Right'
$buttons.Margin = New-Object Windows.Forms.Padding 0

function New-Button([string]$Text, [bool]$Primary) {
    $b = New-Object Windows.Forms.Button
    $b.Text = $Text
    $b.AutoSize = $true
    $b.FlatStyle = 'Flat'
    $b.Cursor = 'Hand'
    $b.Padding = New-Object Windows.Forms.Padding (ScalePx 10), (ScalePx 4), (ScalePx 10), (ScalePx 4)
    $b.Margin = New-Object Windows.Forms.Padding (ScalePx 8), 0, 0, 0
    if ($Primary) {
        $b.BackColor = $ui.Primary
        $b.ForeColor = [Drawing.Color]::White
        $b.FlatAppearance.BorderSize = 0
    } else {
        $b.BackColor = [Drawing.Color]::White
        $b.ForeColor = $ui.Text
        $b.FlatAppearance.BorderColor = $ui.Border
    }
    return $b
}
$statusPageButton = New-Button 'Status page' $false
$updateButton = New-Button 'Check for updates' $true
$buttons.Controls.Add($statusPageButton)
$buttons.Controls.Add($updateButton)

$header.Controls.Add($left, 0, 0)
$header.Controls.Add($buttons, 1, 0)
$root.Controls.Add($header, 0, 0)

# The log, in a dark frame.
$logFrame = New-Object Windows.Forms.Panel
$logFrame.Dock = 'Fill'
$logFrame.BackColor = $ui.LogBack
$logFrame.Padding = New-Object Windows.Forms.Padding (ScalePx 10), (ScalePx 8), (ScalePx 4), (ScalePx 8)
$logFrame.Margin = New-Object Windows.Forms.Padding 0

$logBox = New-Object Windows.Forms.RichTextBox
$logBox.Dock = 'Fill'
$logBox.ReadOnly = $true
$logBox.BorderStyle = 'None'
$logBox.BackColor = $ui.LogBack
$logBox.ForeColor = $logColors.Default
$logBox.Font = $fontLog
$logBox.WordWrap = $true
$logBox.DetectUrls = $false
$logBox.ScrollBars = 'Vertical'
$logFrame.Controls.Add($logBox)
$root.Controls.Add($logFrame, 0, 1)

$footer = New-Object Windows.Forms.Label
$footer.AutoSize = $true
$footer.Font = $fontSmall
$footer.ForeColor = $ui.Muted
$footer.Margin = New-Object Windows.Forms.Padding 0, (ScalePx 8), 0, 0
$footer.Text = 'Closing this window does not stop GB Automations. It keeps running in the background.'
$root.Controls.Add($footer, 0, 2)

# ------------------------------------------------------------------- log

function Get-AnsiColor([string]$Codes, [Drawing.Color]$Current) {
    $color = $Current
    foreach ($code in ($Codes -split ';')) {
        if ($code -eq '' -or $code -eq '0' -or $code -eq '39' -or $code -eq '22') { $color = $logColors.Default }
        elseif ($code -eq '2') { $color = $logColors.Dim }
        elseif ($ansi.ContainsKey($code)) { $color = $ansi[$code] }
    }
    return $color
}

# A batch is a list of @(text, color) pieces, written to the box in one go.
function Add-AppLine($Batch, [string]$Line) {
    $Line = $emojiPattern.Replace($Line, '')
    $color = $logColors.Default
    $pos = 0
    foreach ($m in $ansiPattern.Matches($Line)) {
        if ($m.Index -gt $pos) { $Batch.Add(@($Line.Substring($pos, $m.Index - $pos), $color)) }
        $color = Get-AnsiColor $m.Groups[1].Value $color
        $pos = $m.Index + $m.Length
    }
    # Parenthesised: PowerShell's comma binds tighter than +.
    $Batch.Add(@(($Line.Substring($pos) + "`n"), $color))
}

function Add-PlainLine($Batch, [string]$Line, [Drawing.Color]$Color) { $Batch.Add(@(($Line + "`n"), $Color)) }

# Output of Start-/Update-GbAutomations.ps1. Their Write-Host colours don't
# survive the pipe, so the markers they print (==>, OK, !!) pick the colour.
function Add-ProgramLine($Batch, [string]$Line) {
    if ($Line -like 'GB-CHANGE: *') {
        $text = $Line.Substring(11)
        $state.Changes.Add($text)
        Add-PlainLine $Batch "         $bullet $text" $logColors.Program
        return
    }
    if ($Line -eq 'GB-RESTART-PROGRAM') { $state.RestartProgram = $true; return }
    $color = $logColors.Program
    if ($Line -match '^==> ') { $color = $logColors.Step }
    elseif ($Line -match '^\s+OK\s') { $color = $logColors.Ok }
    elseif ($Line -match '^\s+!!\s') { $color = $logColors.Warn }
    elseif ($Line -match '^(PROBLEM|FAILED)') { $color = $logColors.Error }
    Add-PlainLine $Batch $Line $color
}

function Test-LogAtBottom {
    if ($logBox.TextLength -eq 0) { return $true }
    $bottomChar = $logBox.GetCharIndexFromPosition((New-Object Drawing.Point 1, ($logBox.ClientSize.Height - 2)))
    return ($logBox.GetLineFromCharIndex($bottomChar) -ge ($logBox.GetLineFromCharIndex($logBox.TextLength) - 1))
}

function Write-LogBatch($Batch) {
    if ($Batch.Count -eq 0) { return }
    # Follow new lines only while the view is at the bottom, so scrolling up
    # to read (or selecting text to copy) isn't yanked away.
    $follow = Test-LogAtBottom
    $selStart = $logBox.SelectionStart
    $selLength = $logBox.SelectionLength
    [GbAutomations.Native]::SetRedraw($logBox.Handle, $false)
    try {
        foreach ($piece in $Batch) {
            $logBox.SelectionStart = $logBox.TextLength
            $logBox.SelectionLength = 0
            $logBox.SelectionColor = $piece[1]
            $logBox.AppendText($piece[0])
        }
        # Keep the box from growing without end: drop the oldest lines.
        if ($logBox.GetLineFromCharIndex($logBox.TextLength) -gt 6000) {
            $cut = $logBox.GetFirstCharIndexFromLine(1500)
            $logBox.ReadOnly = $false
            $logBox.Select(0, $cut)
            $logBox.SelectedText = ''
            $logBox.ReadOnly = $true
            $selStart = [Math]::Max(0, $selStart - $cut)
        }
        if ($follow) {
            $logBox.SelectionStart = $logBox.TextLength
            $logBox.SelectionLength = 0
            $logBox.ScrollToCaret()
        } else {
            $logBox.Select($selStart, $selLength)
        }
    } finally {
        [GbAutomations.Native]::SetRedraw($logBox.Handle, $true)
        $logBox.Invalidate()
    }
}

function Write-LogNote([string]$Text, [Drawing.Color]$Color) {
    $batch = New-Object Collections.Generic.List[object]
    Add-PlainLine $batch $Text $Color
    Write-LogBatch $batch
}

# --------------------------------------------------------------- state

$state = @{
    Phase          = 'starting'   # starting | idle | checking | updating
    StartJob       = $null
    UpdateJob      = $null
    UpdateMode     = $null        # check | install
    LogJob         = $null
    LogSince       = $null
    LogRetryAt     = [DateTime]::MinValue
    LogPausedShown = $false
    Changes        = New-Object Collections.Generic.List[string]
    RestartProgram = $false
    Result         = $null
    Closing        = $false
    LastTickError  = $null
}
$health = $null

function Start-PsJob([string]$Script, [string]$Arguments) {
    $path = Join-Path $PSScriptRoot $Script
    return New-Object GbAutomations.Child $psExe, "-NoProfile -ExecutionPolicy Bypass -File `"$path`" $Arguments", $repoRoot
}

function Start-LogStream {
    $logArgs = 'compose logs --follow --no-log-prefix '
    # First connect: recent history for context. After a break (an update, a
    # crash, Docker restarting) carry on exactly where it stopped.
    if ($state.LogSince) { $logArgs += "--since $($state.LogSince) api" } else { $logArgs += '--tail 150 api' }
    try {
        $state.LogJob = New-Object GbAutomations.Child 'docker', $logArgs, $repoRoot
    } catch {
        $state.LogRetryAt = (Get-Date).AddSeconds(10)
    }
}

function Update-Version {
    $v = Get-NativeOutput -Exe 'git' -Arguments @('log', '-1', '--format=%h|%cd', '--date=format:%d %b %Y') -AllowFail
    if ($v -and $v.Contains('|')) {
        $parts = $v.Split('|')
        $versionLabel.Text = "Version $($parts[0])  $middot  $($parts[1])"
    }
}

function Set-Status([string]$Text, [Drawing.Color]$Color) {
    $full = "$dot  $Text"
    if ($statusLabel.Text -ne $full) { $statusLabel.Text = $full }
    if ($statusLabel.ForeColor -ne $Color) { $statusLabel.ForeColor = $Color }
}

function Update-Status {
    switch ($state.Phase) {
        'starting' { Set-Status "Starting$ellipsis" $ui.Amber; return }
        'checking' { Set-Status "Checking for updates$ellipsis" $ui.Amber; return }
        'updating' { Set-Status "Installing update$ellipsis" $ui.Amber; return }
    }
    $h = if ($health) { $health.State } else { -1 }
    if ($h -eq 1) { Set-Status 'Running' $ui.Green }
    elseif ($h -eq 0) { Set-Status 'Not running' $ui.Red }
    else { Set-Status "Checking$ellipsis" $ui.Gray }
}

# ---------------------------------------------------------------- updates

function Start-Update([string]$Mode) {
    $state.Phase = if ($Mode -eq 'check') { 'checking' } else { 'updating' }
    $state.UpdateMode = $Mode
    $state.Changes.Clear()
    $state.RestartProgram = $false
    $updateButton.Enabled = $false
    $updateButton.Text = if ($Mode -eq 'check') { "Checking$ellipsis" } else { "Updating$ellipsis" }
    $arguments = if ($Mode -eq 'check') { '-CheckOnly' } else { '' }
    try {
        $state.UpdateJob = Start-PsJob 'Update-GbAutomations.ps1' $arguments
    } catch {
        Complete-Update
        [Windows.Forms.MessageBox]::Show($form, "Could not run the update check: $($_.Exception.Message)", $programTitle, 'OK', 'Warning') | Out-Null
    }
}

function Complete-Update {
    $state.Phase = 'idle'
    $updateButton.Enabled = $true
    $updateButton.Text = 'Check for updates'
}

function Restart-Program {
    $state.Closing = $true
    if ($state.LogJob) { $state.LogJob.Stop() }
    if ($mutex) { try { $mutex.ReleaseMutex(); $mutex.Dispose() } catch { } ; $script:mutex = $null }
    Start-GbProgram $repoRoot
    $form.Close()
}

# Shown after the timer tick that saw the job finish (via BeginInvoke), so
# the log keeps flowing while the message is open.
function Show-UpdateResult {
    $r = $state.Result
    $state.Result = $null
    if ($null -eq $r) { return }
    if ($r.Mode -eq 'check') {
        if ($r.Code -eq 0) {
            [Windows.Forms.MessageBox]::Show($form, 'GB Automations is up to date.', $programTitle, 'OK', 'Information') | Out-Null
        } elseif ($r.Code -eq 10) {
            $list = ($state.Changes | ForEach-Object { "$bullet $_" }) -join "`n"
            $text = "An update is available:`n`n$list`n`nInstall it now? If it changes the app itself, the app restarts and is offline for about a minute."
            if ([Windows.Forms.MessageBox]::Show($form, $text, $programTitle, 'YesNo', 'Question') -eq 'Yes') { Start-Update 'install' }
        } else {
            [Windows.Forms.MessageBox]::Show($form, 'Could not check for updates. The log in this window shows why.', $programTitle, 'OK', 'Warning') | Out-Null
        }
        return
    }
    switch ($r.Code) {
        0 {
            if ($state.RestartProgram) {
                [Windows.Forms.MessageBox]::Show($form, 'The update is installed. GB Automations will now reopen to finish it.', $programTitle, 'OK', 'Information') | Out-Null
                Restart-Program
            } else {
                [Windows.Forms.MessageBox]::Show($form, "The update is installed.`n`n$($versionLabel.Text)", $programTitle, 'OK', 'Information') | Out-Null
            }
        }
        3 { [Windows.Forms.MessageBox]::Show($form, "The new version did not start correctly, so the previous version was put back. Everything runs as before.`n`nPlease tell the developer.", $programTitle, 'OK', 'Warning') | Out-Null }
        4 { [Windows.Forms.MessageBox]::Show($form, "The update did not start correctly and could not be undone automatically.`n`nPlease tell the developer. The log in this window shows what happened.", $programTitle, 'OK', 'Error') | Out-Null }
        default { [Windows.Forms.MessageBox]::Show($form, 'The update was not installed. The log in this window shows why.', $programTitle, 'OK', 'Warning') | Out-Null }
    }
}

# ------------------------------------------------------------------ timer

function Invoke-Tick {
    $batch = New-Object Collections.Generic.List[object]
    $line = $null

    $job = $state.StartJob
    if ($job) {
        $n = 0
        while ($n -lt 400 -and $job.TryTake([ref]$line)) { $n++; Add-ProgramLine $batch $line }
        if ($job.Finished) {
            $state.StartJob = $null
            $state.Phase = 'idle'
            $updateButton.Enabled = $true
            if ($job.ExitCode -ne 0) { Add-PlainLine $batch 'GB Automations could not start. The lines above say why.' $logColors.Error }
            Add-PlainLine $batch '' $logColors.Dim
            Add-PlainLine $batch '-- Live log of the app --' $logColors.Dim
        }
    }

    $job = $state.UpdateJob
    if ($job) {
        $n = 0
        while ($n -lt 400 -and $job.TryTake([ref]$line)) { $n++; Add-ProgramLine $batch $line }
        if ($job.Finished) {
            $state.UpdateJob = $null
            $state.Result = @{ Mode = $state.UpdateMode; Code = $job.ExitCode }
            Complete-Update
            Update-Version
            [void]$form.BeginInvoke([Action] { Show-UpdateResult })
        }
    }

    if ($state.Phase -ne 'starting' -and -not $Preview) {
        $job = $state.LogJob
        if ($job) {
            # docker exits within a moment, with an error, when Docker itself
            # isn't running: hold the first seconds back so that error isn't
            # printed every few seconds while Docker is down.
            if ($job.HasExited -and $job.ExitCode -ne 0) {
                $job.Discard()
            } elseif ($job.Seconds -ge 2 -or $job.HasExited) {
                $n = 0
                while ($n -lt 600 -and $job.TryTake([ref]$line)) { $n++; Add-AppLine $batch $line }
            }
            if ($job.Finished) {
                $state.LogJob = $null
                if ($job.ExitCode -eq 0) {
                    $state.LogSince = [DateTime]::UtcNow.ToString('yyyy-MM-ddTHH:mm:ssZ')
                }
                if (-not $state.LogPausedShown -and ($job.ExitCode -ne 0 -or $job.Seconds -gt 10)) {
                    Add-PlainLine $batch '-- The app stopped or is restarting. The log continues as soon as it is back. --' $logColors.Dim
                    $state.LogPausedShown = $true
                }
                $state.LogRetryAt = (Get-Date).AddSeconds(5)
            } elseif ($job.Seconds -gt 10) {
                $state.LogPausedShown = $false
            }
        } elseif ((Get-Date) -ge $state.LogRetryAt) {
            Start-LogStream
        }
    }

    Write-LogBatch $batch
    Update-Status
}

$timer = New-Object Windows.Forms.Timer
$timer.Interval = 250
$timer.Add_Tick({
    if ($state.Closing) { return }
    try {
        Invoke-Tick
    } catch {
        # Once per distinct error: this runs four times a second.
        $message = $_.Exception.Message
        if ($message -ne $state.LastTickError) {
            $state.LastTickError = $message
            try { Write-LogNote "(GB Automations window error: $message)" $logColors.Error } catch { }
        }
    }
})

# ---------------------------------------------------------------- events

$statusPageButton.Add_Click({ Start-Process $script:GbStatusUrl })
$updateButton.Add_Click({ if ($state.Phase -eq 'idle') { Start-Update 'check' } })

$form.Add_FormClosing({
    param($s, $e)
    if ($state.Closing) { return }
    if ($state.Phase -eq 'updating') {
        $e.Cancel = $true
        [Windows.Forms.MessageBox]::Show($form, 'An update is being installed. Please wait until it has finished.', $programTitle, 'OK', 'Information') | Out-Null
        return
    }
    $state.Closing = $true
    $timer.Stop()
    if ($state.LogJob) { $state.LogJob.Stop() }
})

$form.Add_Shown({
    $form.Activate()
    if ($Preview) {
        Show-PreviewContent
        return
    }
    Update-Version
    $updateButton.Enabled = $false
    try { Set-GbShortcuts $repoRoot } catch { Write-LogNote "(Could not update the desktop icon: $($_.Exception.Message))" $logColors.Warn }
    $script:health = New-Object GbAutomations.HealthProbe $script:GbHealthUrl
    try {
        $state.StartJob = Start-PsJob 'Start-GbAutomations.ps1' '-NoLiveLog'
    } catch {
        $state.Phase = 'idle'
        Write-LogNote "Could not run the start-up: $($_.Exception.Message)" $logColors.Error
    }
    $timer.Start()
})

# ---------------------------------------------------------------- preview

function Show-PreviewContent {
    $e = [string][char]27
    $versionLabel.Text = "Version c2ccf5f  $middot  13 Nov 2026"
    Set-Status 'Running' $ui.Green
    $batch = New-Object Collections.Generic.List[object]
    foreach ($l in @(
        '', '==> AI tagging (Ollama)', '    OK   Ollama is running', '', '==> Docker', '    OK   Docker is running',
        '', '==> GB Automations', '    OK   Running')) { Add-ProgramLine $batch $l }
    $thread = [char]::ConvertFromUtf32(0x1F9F5)
    $clip = [char]::ConvertFromUtf32(0x1F4CE)
    $check = [string][char]0x2705
    $samples = @(
        "$e[2m10:41:07$e[0m $e[32mINFO $e[0m $e[2mqueue_worker    $e[0m $e[35m[gmail:7f3a] $e[0m$thread sync start: 'Fr$([char]0xF8)en hage' (10 msg) thread=1a19cbea67e18573",
        "$e[2m10:41:08$e[0m $e[32mINFO $e[0m $e[2msync_thread     $e[0m $e[35m[gmail:7f3a] $e[0m  - matched 1 project(s): '1214_Metropolis_Obos Froen hage'",
        "$e[2m10:41:09$e[0m $e[32mINFO $e[0m $e[2msync_thread     $e[0m $e[35m[gmail:7f3a] $e[0m    $clip linked 'Bibliotek - D8001.ifc' from Drive",
        "$e[2m10:41:12$e[0m $e[33mWARN $e[0m $e[2mnotion          $e[0m $e[35m[gmail:7f3a] $e[0mNotion 429: rate limited, retrying in 1.0s",
        "$e[2m10:41:19$e[0m $e[32mINFO $e[0m $e[2msync_thread     $e[0m $e[35m[gmail:7f3a] $e[0m$check sync done in 11.3s: +1 row(s), 10 already present",
        "$e[2m10:42:00$e[0m $e[31mERROR$e[0m $e[2mfiken           $e[0m $e[94m[notion:2c1e] $e[0mFiken 400 POST /invoices/drafts: incomeAccount missing",
        "$e[2m10:42:03$e[0m $e[32mINFO $e[0m $e[2mscheduler       $e[0m $([char]0x23F0) fiken_graduations cron: scanned=41 enqueued=3 skipped_terminal=38"
    )
    foreach ($s in $samples) { Add-AppLine $batch $s }
    Write-LogBatch $batch
    if ($ScreenshotPath) {
        $form.TopMost = $true
        $form.Activate()
        $script:snap = New-Object Windows.Forms.Timer
        $script:snap.Interval = 900
        $script:snap.Add_Tick({
            $script:snap.Stop()
            $b = $form.Bounds
            $bmp = New-Object Drawing.Bitmap $b.Width, $b.Height
            $g = [Drawing.Graphics]::FromImage($bmp)
            $g.CopyFromScreen($b.Location, [Drawing.Point]::Empty, $b.Size)
            $g.Dispose()
            $bmp.Save($ScreenshotPath, [Drawing.Imaging.ImageFormat]::Png)
            $bmp.Dispose()
            $state.Closing = $true
            $form.Close()
        })
        $script:snap.Start()
    }
}

[Windows.Forms.Application]::Run($form)
if ($mutex) { try { $mutex.ReleaseMutex() } catch { } }

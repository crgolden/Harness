<#
.SYNOPSIS
Measures what the registered PreToolUse and PostToolUse hooks cost one tool call, as Claude Code runs them.

.DESCRIPTION
Reads the hooks block of settings.json, picks every hook whose matcher matches each probe's tool the way Claude
Code reads a matcher (empty or "*" matches all; only letters, digits, "_", "-", spaces, "," and "|" is a list of
exact names; anything else is an unanchored, case-sensitive regex), starts all of them at once with the probe's payload on stdin, and times the call from
the first start to the last exit. The host runs matching hooks in parallel, so a call costs its slowest hook plus
whatever the processes cost each other: interpreters started together compete for the cores, and that contention
is what pushes a hook past its timeout on a busy machine. A bench that runs one hook alone cannot see it.

Every probe judges an empty fixture transcript, so no session's own state (a peer reply it owes, an unread repo
guide) makes a gate deny early and skew the timing. Each load worker ends itself when this script's process is
gone or its time bound passes, so a killed bench leaves no core pegged.

Each probe is measured idle and again with one CPU-bound worker per core, round-robin after one warm-up pass, so
the first probe does not absorb the cold start. Production cost under real load is hook_latency_report.py's job;
this answers how the registered shape behaves when the machine is idle and when it is saturated.

.EXAMPLE
pwsh -File Measure-HookLatency.ps1

.EXAMPLE
pwsh -File Measure-HookLatency.ps1 -Iterations 20
#>
[CmdletBinding()]
param(
    [string] $SettingsPath = (Join-Path $env:USERPROFILE '.claude\settings.json'),
    [int]    $Iterations   = 10
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

. (Join-Path $PSScriptRoot 'ClaudeTemp.ps1')
$settings = Get-Content -LiteralPath $SettingsPath -Raw | ConvertFrom-Json
$fixture = New-ClaudeTempPath ('hook-latency-{0}' -f [Guid]::NewGuid().ToString('N'))
$null = New-Item -ItemType Directory -Path $fixture
$transcript = Get-Item -LiteralPath (New-Item -ItemType File -Path (Join-Path $fixture 'bench-session.jsonl'))
$workspace = python (Join-Path $PSScriptRoot 'session_home.py') workspace
$testingDoc = [string](python (Join-Path $PSScriptRoot 'session_home.py') doc 'AGENTS/TESTING.md')
$indexDoc = [string](python (Join-Path $PSScriptRoot 'session_home.py') doc 'AGENTS.md')
if (-not $testingDoc -or -not $indexDoc) { throw 'the workspace holds no AGENTS/TESTING.md or AGENTS.md to time the doc hooks against (Hooks\session_home.py doc)' }

$probes = @(
    @{ Event = 'PreToolUse'; Tool = 'PowerShell'; Input = @{ command = 'dotnet --version' } }
    @{ Event = 'PreToolUse'; Tool = 'Edit'; Input = @{ file_path = $testingDoc; old_string = 'zz-none'; new_string = 'plain words' } }
    @{ Event = 'PreToolUse'; Tool = 'Read'; Input = @{ file_path = $indexDoc } }
    @{ Event = 'PostToolUse'; Tool = 'Edit'; Input = @{ file_path = $testingDoc; old_string = 'zz-none'; new_string = 'plain words' } }
)

function Test-Matcher([string] $Matcher, [string] $Tool) {
    if ($Matcher -in @('', '*')) { return $true }
    if ($Matcher -cmatch '^[A-Za-z0-9_ ,|-]+$') {
        return ($Matcher -split '[|,]' | ForEach-Object { $_.Trim() }) -ccontains $Tool
    }
    return $Tool -cmatch $Matcher
}

function Get-MatchingHooks([string] $Event, [string] $Tool) {
    $entries = $settings.hooks.PSObject.Properties[$Event]
    if (-not $entries) { return @() }
    foreach ($entry in $entries.Value) {
        $matcher = if ($entry.PSObject.Properties['matcher']) { [string] $entry.matcher } else { '' }
        if (Test-Matcher $matcher $Tool) {
            foreach ($hook in $entry.hooks) { $hook }
        }
    }
}

function Invoke-Call($Probe) {
    $hooks = @(Get-MatchingHooks $Probe.Event $Probe.Tool)
    $payload = @{ hook_event_name = $Probe.Event; tool_name = $Probe.Tool; tool_input = $Probe.Input
                  transcript_path = $transcript.FullName; cwd = $workspace; tool_response = @{} } |
        ConvertTo-Json -Compress -Depth 6
    $watch = [Diagnostics.Stopwatch]::StartNew()
    $processes = foreach ($hook in $hooks) {
        $start = [Diagnostics.ProcessStartInfo]::new([string] $hook.command)
        if ($hook.PSObject.Properties['args']) { foreach ($argument in $hook.args) { $start.ArgumentList.Add([string] $argument) } }
        $start.RedirectStandardInput = $true
        $start.RedirectStandardOutput = $true
        $start.RedirectStandardError = $true
        $start.UseShellExecute = $false
        $process = [Diagnostics.Process]::Start($start)
        $reads = @($process.StandardOutput.ReadToEndAsync(), $process.StandardError.ReadToEndAsync())
        $process.StandardInput.Write($payload)
        $process.StandardInput.Close()
        [pscustomobject]@{ Process = $process; Reads = $reads }
    }
    foreach ($running in $processes) {
        $running.Process.WaitForExit()
        $null = [Threading.Tasks.Task]::WaitAll([Threading.Tasks.Task[]] $running.Reads)
    }
    $watch.Stop()
    [pscustomobject]@{ Hooks = $hooks.Count; Milliseconds = $watch.Elapsed.TotalMilliseconds }
}

function Measure-Round([string] $Label) {
    foreach ($probe in $probes) { $null = Invoke-Call $probe }
    $samples = @{}
    $counts = @{}
    for ($i = 0; $i -lt $Iterations; $i++) {
        foreach ($probe in $probes) {
            $key = '{0} {1}' -f $probe.Event, $probe.Tool
            $result = Invoke-Call $probe
            $counts[$key] = $result.Hooks
            $samples[$key] = @($samples[$key]) + $result.Milliseconds | Where-Object { $null -ne $_ }
        }
    }
    Write-Host ''
    Write-Host "== $Label"
    foreach ($key in $samples.Keys | Sort-Object) {
        $sorted = @($samples[$key] | Sort-Object)
        $median = $sorted[[int][Math]::Floor($sorted.Count / 2)]
        $p90 = $sorted[[Math]::Min($sorted.Count - 1, [int][Math]::Floor($sorted.Count * 0.9))]
        Write-Host ('{0,-24} {1} hook process(es)  median={2,7:N0}ms  p90={3,7:N0}ms' -f $key, $counts[$key], $median, $p90)
    }
}

Write-Host "settings: $SettingsPath"
Write-Host "transcript: $($transcript.FullName)"
Write-Host "iterations per probe: $Iterations"
Measure-Round 'idle'

$registeredTimeouts = foreach ($event in 'PreToolUse', 'PostToolUse') {
    $entries = $settings.hooks.PSObject.Properties[$event]
    if ($entries) {
        foreach ($hook in $entries.Value.hooks) { if ($hook.PSObject.Properties['timeout']) { [int] $hook.timeout } }
    }
}
$hookTimeoutSeconds = if ($registeredTimeouts) { ($registeredTimeouts | Measure-Object -Maximum).Maximum } else { 600 }
$minutes = [Math]::Ceiling($Iterations * $probes.Count * $hookTimeoutSeconds / 60) + 2
$started = (Get-Process -Id $PID).StartTime.ToUniversalTime().Ticks
$spin = "`$parent = $PID; `$started = $started; `$end = [DateTime]::UtcNow.AddMinutes($minutes); " +
    "while ([DateTime]::UtcNow -lt `$end) { for (`$i = 0; `$i -lt 200000; `$i++) { }; " +
    "try { if ([Diagnostics.Process]::GetProcessById(`$parent).StartTime.ToUniversalTime().Ticks -ne `$started) { break } } catch { break } }"
$workers = 1..[Environment]::ProcessorCount | ForEach-Object {
    Start-Process -FilePath 'pwsh' -ArgumentList '-NoProfile', '-Command', $spin -WindowStyle Hidden -PassThru
}
$loadClock = [Diagnostics.Stopwatch]::StartNew()
try {
    Measure-Round "under load: $([Environment]::ProcessorCount) CPU-bound workers"
    $alive = @($workers | Where-Object { -not $_.HasExited }).Count
    Write-Host ('load round took {0:N0} s of a {1} min worker bound; workers still loading at its end: {2} of {3}' -f
        $loadClock.Elapsed.TotalSeconds, $minutes, $alive, $workers.Count)
}
finally {
    $workers | ForEach-Object { Stop-Process -Id $_.Id -Force -ErrorAction SilentlyContinue }
    Remove-Item -LiteralPath $fixture -Recurse -Force -ErrorAction SilentlyContinue
}

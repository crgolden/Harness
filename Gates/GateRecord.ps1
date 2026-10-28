$script:gateVerdictsFile = $null
$script:gateRecordFile = $null
$script:gateRepoPath = $null
$script:gateRepoName = $null
$script:gateStandalone = $false
$script:gateStartedAt = $null
$script:gateStartedAtMs = $null
$script:gateRunRows = [Collections.Generic.List[object]]::new()
$script:gateCurrentStep = $null
$script:gateCurrentStepStartedAt = $null
$script:gateStateWriteAttempts = 20
$script:gateStateWriteRetryMilliseconds = 100
$script:gateLandingFile = $null

function Add-GateRunRow([string]$name, [string]$verdict, [string]$detail, $seconds = $null) {
    $script:gateRunRows.Add([ordered]@{ name = $name; verdict = $verdict; detail = $detail; seconds = $seconds })
}

function Get-StepSeconds {
    if (-not $script:gateCurrentStepStartedAt) { return $null }
    [Math]::Round(([DateTimeOffset]::UtcNow - [DateTimeOffset]::Parse($script:gateCurrentStepStartedAt, [Globalization.CultureInfo]::InvariantCulture)).TotalSeconds, 1)
}

function Save-GateRun([string]$verdict) {
    <#
    .SYNOPSIS
        Writes the gate record through a temporary file and a rename, so a reader polling it mid-run (gate_status.py,
        watch_gate.py) never sees a half-written file. A RUNNING record carries every finished row and the step in
        flight, which is how a session follows a gate without a process id.
    #>
    if (-not $script:gateRecordFile) { return }
    if ((Test-GateRecordIsLanding) -and (Get-GateSupersedingLanding)) { return }
    $running = $verdict -eq 'RUNNING'
    $record = [ordered]@{
        repo        = $script:gateRepoName
        verdict     = $verdict
        startedAt   = $script:gateStartedAt
        completedAt = if ($running) { $null } else { (Get-Date).ToUniversalTime().ToString('o') }
        pid         = $PID
        currentStep = if ($running) { $script:gateCurrentStep } else { $null }
        currentStepStartedAt = if ($running) { $script:gateCurrentStepStartedAt } else { $null }
        machine     = $env:COMPUTERNAME
        session     = $env:CLAUDE_CODE_SESSION_ID
        fingerprint = $script:gateFingerprint
        tools       = $script:gateToolHashes
        toolsRoot   = $script:gateToolsRoot
        repoPath    = $script:gateRepoPath
        standalone  = $script:gateStandalone
        steps       = @($script:gateRunRows)
        volatile    = @($script:gateVolatileSteps)
        manifest    = $script:gateManifest
    }
    Write-GateStateFile $script:gateRecordFile ($record | ConvertTo-Json -Depth 5)
}

function Get-MainWorktreeStatePath([string]$repoPath, [string]$directory, [string]$leaf) {
    <#
    .SYNOPSIS
        Where the repo's main checkout keeps the state file named $leaf, mapped from this checkout's state directory
        the way GateCommon lays both out, or $null when it cannot be told. In the main checkout itself it is this
        checkout's own file.
    #>
    $listing = @(git -C $repoPath worktree list --porcelain)
    if ($LASTEXITCODE -ne 0 -or $listing.Count -eq 0 -or -not $listing[0].StartsWith('worktree ', [StringComparison]::Ordinal)) { return $null }
    $main = [IO.Path]::GetFullPath($listing[0].Substring('worktree '.Length)).TrimEnd('\')
    $here = [IO.Path]::GetFullPath($repoPath).TrimEnd('\')
    if ($main -eq $here) { return Join-Path $directory $leaf }
    if ([string]::Equals([IO.Path]::GetFullPath($directory).TrimEnd('\'), (Get-GateRecordsDirectory $here).TrimEnd('\'), [StringComparison]::OrdinalIgnoreCase)) {
        return Join-Path (Get-GateRecordsDirectory $main) $leaf
    }
    $relative = [IO.Path]::GetRelativePath((Split-Path -Parent $here), [IO.Path]::GetFullPath($directory))
    if ([IO.Path]::IsPathRooted($relative) -or $relative -eq '..' -or $relative.StartsWith('..\', [StringComparison]::Ordinal)) { return $null }
    Join-Path (Join-Path (Split-Path -Parent $main) $relative) $leaf
}

function Get-MainWorktreeVerdicts([string]$repo, [string]$repoPath, [string]$directory) {
    $file = Get-MainWorktreeStatePath $repoPath $directory "$repo.$env:COMPUTERNAME.json"
    if (-not $file -or [string]::Equals([IO.Path]::GetFullPath($file), [IO.Path]::GetFullPath((Join-Path $directory "$repo.$env:COMPUTERNAME.json")), [StringComparison]::OrdinalIgnoreCase)) { return @{} }
    if (-not (Test-Path -LiteralPath $file -PathType Leaf)) { return @{} }
    $loaded = Get-Content -LiteralPath $file -Raw | ConvertFrom-Json -AsHashtable
    if ($loaded) { return $loaded }
    return @{}
}

function Write-GateStateFile([string]$path, [string]$json) {
    $staging = "$path.$PID.tmp"
    Set-Content -LiteralPath $staging -Value $json -Encoding utf8
    for ($attempt = 1; ; $attempt++) {
        try {
            [IO.File]::Move($staging, $path, $true)
            return
        } catch [IO.IOException], [UnauthorizedAccessException] {
            if ($attempt -ge $script:gateStateWriteAttempts) {
                Remove-Item -LiteralPath $staging -Force -ErrorAction SilentlyContinue
                Write-Host "GATE: FAILED (could not write $path after $attempt attempts: $($_.Exception.Message))"
                exit 1
            }
            [Threading.Thread]::Sleep($script:gateStateWriteRetryMilliseconds)
        }
    }
}

function Get-CleanRepoRefusal([string]$repoPath) {
    $global:LASTEXITCODE = $null
    $status = @(git -C $repoPath status --porcelain --untracked-files=all)
    if ($LASTEXITCODE -ne 0) { return "git status failed in $repoPath (exit $LASTEXITCODE), so its uncommitted changes cannot be shown" }
    $pending = @($status | Where-Object { $_.Substring(3).Trim('"') -notmatch $script:gateLeftoverPattern })
    if ($pending.Count -eq 0) { return "$repoPath has no uncommitted changes, and a gate never runs on a clean repo" }
    $null
}

function Get-GateStateDirectoryRefusal([string]$repoPath, [string]$directory) {
    $full = [IO.Path]::GetFullPath($directory).TrimEnd('\')
    if ($full -notmatch '(^|\\)\.claude(\\|$)') { return $null }
    "gate state never goes under a .claude folder, where every write prompts; it belongs in $(Get-GateRecordsDirectory $repoPath), never $full"
}

function Get-DefaultGateStateDirectory([string]$repoPath) {
    if ([string]::IsNullOrWhiteSpace($env:GATE_RUN_KIND)) { return Get-GateRecordsDirectory $repoPath }
    return Join-Path (Split-Path -Parent $repoPath) 'gates'
}

function Initialize-GateState([string]$repo, [string]$repoPath, [string]$directory, [switch]$Standalone, [string]$Probe) {
    $script:gateStandalone = [bool]$Standalone
    if (-not [string]::IsNullOrWhiteSpace($Probe)) {
        $requested = @($Probe -split ',' | ForEach-Object { $_.Trim() } | Where-Object { $_ })
        $known = @($script:gateOriginStep, $script:gateDependencyOrderStep) + @($script:gateCatalogToolFiles.Keys) + @($script:gateDeclaredSteps)
        $unknown = @($requested | Where-Object { $_ -cnotin $known })
        if (-not $requested.Count -or $unknown.Count) {
            Write-Host "PROBE: FAILED (-Probe names no step or a step this gate does not have: $(($unknown | ForEach-Object { "'$_'" }) -join ', '); its steps: $(($known | ForEach-Object { "'$_'" }) -join ', '))"
            exit 1
        }
        $script:gateProbeSteps = $requested
    }
    $label = if ($null -ne $script:gateProbeSteps) { 'PROBE' } else { 'GATE' }
    if (-not (Test-Path -LiteralPath (Join-Path $repoPath '.git'))) {
        Write-Host "${label}: FAILED ($repoPath is not a repository root)"
        exit 1
    }
    if ([string]::IsNullOrWhiteSpace($directory)) { $directory = Get-DefaultGateStateDirectory $repoPath }
    $misplaced = Get-GateStateDirectoryRefusal $repoPath $directory
    if ($misplaced) {
        Write-Host "${label}: FAILED ($misplaced)"
        exit 1
    }
    $refusal = Get-CleanRepoRefusal $repoPath
    if ($refusal) {
        Write-Host "${label}: NOT RUN ($refusal)"
        exit 1
    }
    if ($null -ne $script:gateProbeSteps) {
        $script:gateSlot = Enter-GateSlotForGate $repo 'PROBE: FAILED'
        $script:gateRepoName = $repo
        $script:gateRepoPath = $repoPath
        $script:gateManifest = Get-GateManifest $repoPath
        Write-Host "PROBE: running $($script:gateProbeSteps -join ', ') only; no gate record, verdict or carry is written"
        Assert-StepInputsDeclared
        return
    }
    $script:gateSlot = Enter-GateSlotForGate $repo
    New-Item -ItemType Directory -Force -Path $directory | Out-Null
    if ([string]::IsNullOrWhiteSpace($env:COMPUTERNAME)) {
        Write-Host 'GATE: FAILED (COMPUTERNAME is unset, so no gate record can be named for this machine)'
        exit 1
    }
    $script:gateVerdictsFile = Join-Path $directory "$repo.$env:COMPUTERNAME.json"
    $script:gateRecordFile = Join-Path $directory "$repo.$env:COMPUTERNAME.last.json"
    $script:gateRepoName = $repo
    $script:gateRepoPath = $repoPath
    $script:gateStartedAt = (Get-Date).ToUniversalTime().ToString('o')
    $script:gateStartedAtMs = [DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds()
    $script:gateLandingFile = if ([string]::IsNullOrWhiteSpace($env:GATE_RUN_KIND)) { Get-MainWorktreeStatePath $repoPath $directory "$repo.$env:COMPUTERNAME.last.json" } else { $null }
    Invoke-OriginFastForward $repoPath
    $script:gateManifest = Get-GateManifest $repoPath
    $script:gateToolHashes = Get-GateToolHashes
    $script:gateFingerprint = Get-GateFingerprint $repoPath $script:gateManifest $script:gateToolHashes
    if (Test-Path $script:gateVerdictsFile) {
        $loaded = Get-Content $script:gateVerdictsFile -Raw | ConvertFrom-Json -AsHashtable
        if ($loaded) { $script:gateVerdicts = $loaded }
    }
    $inherited = if ([string]::IsNullOrWhiteSpace($env:GATE_RUN_KIND)) { Get-MainWorktreeVerdicts $repo $repoPath $directory } else { @{} }
    foreach ($step in @($inherited.Keys)) {
        if (-not $script:gateVerdicts.ContainsKey($step)) { $script:gateVerdicts[$step] = $inherited[$step] }
    }
    if ([string]::IsNullOrWhiteSpace($env:GATE_RUN_KIND) -and [string]::Equals([IO.Path]::GetFullPath($directory).TrimEnd('\'), [IO.Path]::GetFullPath((Get-GateRecordsDirectory $repoPath)).TrimEnd('\'), [StringComparison]::OrdinalIgnoreCase)) {
        $script:gateSiblingVerdicts = Get-SiblingVerdicts $repo $directory
    }
    Save-GateRun 'RUNNING'
    Write-Host ("inputs fingerprint {0}" -f $script:gateFingerprint.Substring(0, 12))
    Assert-StepInputsDeclared
}

param(
    [string]$Goal,
    [string]$Probe,
    [string]$Workspace = $(python (Join-Path $PSScriptRoot 'Hooks\session_home.py') workspace),
    [switch]$Standalone
)

$ErrorActionPreference = 'Continue'
$gateCommon = Join-Path $PSScriptRoot 'Gates\GateCommon.ps1'
. $gateCommon
$repo = $PSScriptRoot
$env:TZ = 'UTC'
if ($env:TZ -ne 'UTC') { Write-Host 'GATE: FAILED (TZ pin)'; exit 1 }
Set-Location $repo
if ($Standalone) {
    Remove-Item -Path 'env:CRGOLDEN_WORKSPACE' -ErrorAction SilentlyContinue
    $env:TOOLS_GATE_STANDALONE = '1'
    Write-Host 'Standalone: the fleet rows that read the crgolden workspace are skipped, and the record says so'
} elseif (Test-Path -LiteralPath $Workspace -PathType Container) {
    $env:CRGOLDEN_WORKSPACE = (Get-Item -LiteralPath $Workspace).FullName
    Remove-Item -Path 'env:TOOLS_GATE_STANDALONE' -ErrorAction SilentlyContinue
} else {
    Write-Host "GATE: FAILED (no fleet workspace at '$Workspace': pass -Workspace <crgolden folder> or set CRGOLDEN_WORKSPACE, or pass -Standalone to skip the fleet rows)"
    exit 1
}
$gateState = Get-GateRecordsDirectory $repo
New-Item -ItemType Directory -Force -Path $gateState | Out-Null
$global:LASTEXITCODE = $null
$planText = & python (Join-Path $repo 'Gates\step_inputs.py') $repo --plan --cache (Join-Path $gateState 'Harness.step-plan.cache.json')
if ($LASTEXITCODE -ne 0 -or [string]::IsNullOrWhiteSpace($planText)) {
    Write-Host "GATE: FAILED (Gates\step_inputs.py could not plan the steps: python Gates\step_inputs.py $repo --plan says why)"
    exit 1
}
$plan = $planText | ConvertFrom-Json
foreach ($warning in @($plan.warnings)) { Write-Host "WARN  $warning" }
$stepInputs = @{}
$harnesses = [ordered]@{}
foreach ($step in $plan.steps) {
    $stepInputs[$step.name] = [string[]]@($step.inputs)
    $harnesses[$step.name] = [string[]]@($step.command)
}
Register-GateSteps @($harnesses.Keys)
Register-StepInputs $stepInputs
$durationsFile = Join-Path $gateState "Harness.$env:COMPUTERNAME.durations.json"
$durations = [ordered]@{ startedAt = [DateTimeOffset]::UtcNow.ToString('o'); steps = [ordered]@{} }
function Save-StepSeconds([string]$step, [Diagnostics.Stopwatch]$clock) {
    if ($Probe) { return }
    $durations.steps[$step] = [Math]::Round($clock.Elapsed.TotalSeconds, 1)
    $durations | ConvertTo-Json -Depth 3 | Set-Content -LiteralPath $durationsFile -Encoding utf8
}
Initialize-GateState 'Harness' $repo $gateState -Standalone:$Standalone -Probe $Probe
Resolve-OwedSteps
$clock = [Diagnostics.Stopwatch]::StartNew()
Invoke-CatalogSteps
Save-StepSeconds 'Origin, dependency order and both catalogs' $clock

$stepLogs = Join-Path $gateState $(if ($Probe) { 'probe' } else { 'steps' })
New-Item -ItemType Directory -Force -Path $stepLogs | Out-Null
foreach ($step in (Get-StepRunOrder @($harnesses.Keys))) {
    if (Test-StepCarried $step) { continue }
    $command = $harnesses[$step]
    $stepLog = Join-Path $stepLogs (($step -replace '[^A-Za-z0-9]+', '-').Trim('-') + '.log')
    $clock = [Diagnostics.Stopwatch]::StartNew()
    $global:LASTEXITCODE = $null
    & $command[0] $command[1..($command.Count - 1)] *>&1 | Tee-Object -FilePath $stepLog | Out-Host
    Save-StepSeconds $step $clock
    if ($null -ne $global:LASTEXITCODE -and $global:LASTEXITCODE -ne 0) {
        $failing = @(Select-String -LiteralPath $stepLog -Pattern '^\s*(FAIL|MISMATCH|ERROR)\b' | Select-Object -First 5 |
                ForEach-Object { $_.Line.Trim().Substring(0, [Math]::Min(200, $_.Line.Trim().Length)) })
        Stop-Gate $step ("exit {0}; failing: {1}; full output: {2}" -f $global:LASTEXITCODE,
            $(if ($failing.Count) { $failing -join ' | ' } else { 'no FAIL line matched' }), $stepLog)
    }
    $null = Test-Exit $step
}

Complete-Gate

param([string]$Case, [string]$Root)
if ([string]::IsNullOrWhiteSpace($env:GATE_PROBE_TEMP)) { throw 'GATE_PROBE_TEMP is unset: the probe never takes the account''s real gate slot' }
Remove-Item Env:CLAUDE_CODE_TMPDIR -ErrorAction SilentlyContinue
$env:TEMP = $env:GATE_PROBE_TEMP
. (Join-Path $PSScriptRoot 'GateSlot.ps1')
. (Join-Path $PSScriptRoot 'GateExit.ps1')
. (Join-Path $PSScriptRoot 'GateOrigin.ps1')
. (Join-Path $PSScriptRoot 'GateSteps.ps1')
. (Join-Path $PSScriptRoot 'GateCarry.ps1')
. (Join-Path $PSScriptRoot 'GateRecord.ps1')
. (Join-Path $PSScriptRoot 'GateLanding.ps1')
. (Join-Path $PSScriptRoot 'GateRun.ps1')
. (Join-Path $PSScriptRoot 'GateCatalog.ps1')
. (Join-Path $PSScriptRoot 'SelectedTests.ps1')
$repo = Join-Path $Root 'repo'
$fixtures = Join-Path $Root 'fixtures'
if ($Case -eq 'state-write') {
    Write-GateStateFile (Join-Path $Root 'held.json') '{"written": true}'
    Write-Host 'RESULT written'
    exit 0
}
if ($Case -like 'run-tools*') {
    $script:gateToolsRoot = Join-Path $Root 'tools'
}
if ($Case -like 'run-volatile*') {
    Register-VolatileSteps @('First step')
}
if ($Case -like 'run-step-inputs*') {
    Register-StepInputs @{ 'First step' = @('src/*') }
}
if ($Case -like 'run-*') {
    $env:GATE_RUN_KIND = switch ($Case) { 'run-origin-triage' { 'triage' } 'run-origin-unknown' { 'Triage' } default { $null } }
    $env:CLAUDE_CODE_SESSION_ID = if ($Case -eq 'run-origin-session') { 'session-under-test' } else { $null }
    Initialize-GateState 'repo' $repo (Join-Path $Root 'state')
}
if ($Case -like 'sibling-*') {
    $sibling = Join-Path $Root 'repo-sibling'
    if ($Case -ceq 'sibling-copy') {
        if (Test-Path -LiteralPath $sibling) { Remove-Item -LiteralPath $sibling -Recurse -Force }
        Copy-Item -LiteralPath $repo -Destination $sibling -Recurse -Force
    }
    if ($Case -ceq 'sibling-edited') { Add-Content -LiteralPath (Join-Path $sibling 'src\code.cs') -Value 'class SiblingOnly {}' }
    if ($Case -ceq 'sibling-reverted') { Copy-Item -LiteralPath (Join-Path $repo 'src\code.cs') -Destination (Join-Path $sibling 'src\code.cs') -Force }
    Register-StepInputs @{ 'First step' = @('src/*') }
    $checkout = if ($Case -ceq 'sibling-first') { $repo } else { $sibling }
    Initialize-GateState 'sibling-repo' $checkout
    if ($Case -ceq 'sibling-fail') { $global:LASTEXITCODE = 1; $null = Test-Exit 'First step' }
    if (Test-StepCarried 'First step') { Write-Host 'RESULT carried' }
    else { $global:LASTEXITCODE = 0; $null = Test-Exit 'First step'; Write-Host 'RESULT ran' }
    Complete-Gate
}
if ($Case -like 'worktree-*') {
    $env:GATE_RUN_KIND = if ($Case -eq 'worktree-triage-pass') { 'triage' } else { $null }
    Initialize-GateState 'repo' $repo
    if ($Case -eq 'worktree-fail') { $global:LASTEXITCODE = 1; $null = Test-Exit 'First step' }
    if (Test-StepCarried 'First step') { Write-Host 'RESULT carried' }
    else { $global:LASTEXITCODE = 0; $null = Test-Exit 'First step'; Write-Host 'RESULT ran' }
    Complete-Gate
}
if ($Case -eq 'progress') {
    $state = Join-Path $Root 'state-progress'
    Initialize-GateState 'repo' $repo $state
    $record = Join-Path $state "repo.$env:COMPUTERNAME.last.json"
    $null = Test-StepCarried 'First step'
    Write-Host "SNAP-RUNNING $(Get-Content -LiteralPath $record -Raw | ConvertFrom-Json | ConvertTo-Json -Compress -Depth 5)"
    $global:LASTEXITCODE = 0
    $null = Test-Exit 'First step'
    Write-Host "SNAP-ROW $(Get-Content -LiteralPath $record -Raw | ConvertFrom-Json | ConvertTo-Json -Compress -Depth 5)"
    Complete-Gate
}
if ($Case -like 'inputs-*') {
    Register-GateSteps @('First step', 'Second step')
    switch ($Case) {
        'inputs-complete' { Register-StepInputs @{ 'First step' = @('src/*'); 'Second step' = @('*') } }
        'inputs-undeclared' { Register-StepInputs @{ 'First step' = @('src/*') } }
        'inputs-dead-pattern' { Register-StepInputs @{ 'First step' = @('absent/*'); 'Second step' = @('*') } }
        'inputs-unregistered' { Register-StepInputs @{ 'First step' = @('src/*'); 'Second step' = @('*'); 'Frist step' = @('*') } }
        'inputs-volatile-unregistered' { Register-StepInputs @{ 'First step' = @('src/*'); 'Second step' = @('*') }; Register-VolatileSteps @('Frist step') }
        'inputs-volatile-registered' { Register-StepInputs @{ 'First step' = @('src/*'); 'Second step' = @('*') }; Register-VolatileSteps @('Second step') }
    }
    Initialize-GateState 'repo' $repo (Join-Path $Root "state-$Case")
    Write-Host 'RESULT initialized'
    Complete-Gate
}
if ($Case -like 'state-dir-*') {
    $env:GATE_RUN_KIND = if ($Case -ceq 'state-dir-triage-claude' -or $Case -ceq 'state-dir-triage-default') { 'triage' } else { $null }
    $state = switch ($Case) {
        'state-dir-checkout-claude' { Join-Path $repo '.claude\gates' }
        'state-dir-workspace-claude' { Join-Path $Root '.claude\gates' }
        'state-dir-triage-claude' { Join-Path $Root '.claude\gates' }
        'state-dir-default' { Get-DefaultGateStateDirectory $repo }
        'state-dir-triage-default' { Get-DefaultGateStateDirectory $repo }
        'state-dir-claudeish' { Join-Path $Root 'my.claude-notes\gates' }
    }
    Write-Host "RESULT $((Get-GateStateDirectoryRefusal $repo $state) ?? 'none')"
    if ($Case -ceq 'state-dir-default') { Write-Host "DEFAULT $state" }
    if ($Case -ceq 'state-dir-triage-default') {
        $beside = Join-Path (Split-Path -Parent $repo) 'gates'
        Write-Host "STATE triage-beside-worktrees=$([string]::Equals($state, $beside, [StringComparison]::OrdinalIgnoreCase) -and $state -notmatch '\\\.claude\\')"
    }
    exit 0
}

function Invoke-ProbeStep([string]$name) {
    if (Test-StepCarried $name) { return 'carried' }
    $global:LASTEXITCODE = 0
    $null = Test-Exit $name
    return 'ran'
}
Register-GateSteps @('First step', 'Second step', 'Third step')
if ($Case -like 'steps-*') {
    $script:gateCatalogToolFiles = [ordered]@{}
    Register-GateSteps @('First step', 'Second step')
    Register-StepInputs @{ 'First step' = @('src/*'); 'Second step' = @('docs/*') }
    if ($Case -ceq 'steps-owed') { $script:gateToolsRoot = Join-Path $Root 'tools' }
    Initialize-GateState 'repo' $repo (Join-Path $Root $(if ($Case -ceq 'steps-owed') { 'state-owed' } else { 'state-steps' }))
    $requested = switch ($Case) {
        'steps-none' { $null }
        'steps-unknown' { @('Third step') }
        'steps-both' { @('First step,Second step') }
        'steps-first' { @('First step') }
        'steps-second' { @('Second step') }
    }
    if ($Case -ceq 'steps-owed') { Resolve-OwedSteps } else { Assert-RequestedSteps $requested }
    foreach ($step in 'First step', 'Second step') { Write-Host "STEP $step $(Invoke-ProbeStep $step)" }
    Complete-Gate
}
if ($Case -like 'probe-*') {
    $script:gateCatalogToolFiles = [ordered]@{}
    Register-GateSteps @('First step', 'Second step')
    Register-StepInputs @{ 'First step' = @('src/*'); 'Second step' = @('docs/*') }
    $probe = switch ($Case) { 'probe-first' { 'First step' } 'probe-fail' { 'Second step' } 'probe-unknown' { 'Third step' } default { $null } }
    Initialize-GateState 'repo' $repo (Join-Path $Root 'state-probe') -Probe $probe
    foreach ($step in 'First step', 'Second step') {
        if (Test-StepCarried $step) { Write-Host "STEP $step skipped" }
        else { $global:LASTEXITCODE = $(if ($Case -ceq 'probe-fail') { 1 } else { 0 }); $null = Test-Exit $step; Write-Host "STEP $step ran" }
    }
    Complete-Gate
}
function Write-ProbeLanding([long]$atMs) {
    $landing = [ordered]@{ repo = 'repo'; verdict = 'LANDED'; startedAt = 'probe'; completedAt = 'probe'; landedAt = 'probe'
        landedAtMs = $atMs; head = '0123456789abcdef0123'; steps = @() }
    Write-GateStateFile $script:gateRecordFile ($landing | ConvertTo-Json -Depth 3)
}
if ($Case -like 'order-*') {
    Register-GateSteps @('First step', 'Second step')
    Register-StepInputs @{ 'First step' = @('src/*'); 'Second step' = @('docs/*') }
    $script:gateRepoPath = $repo
    $order = switch ($Case) {
        'order-touched-docs' { Get-StepRunOrder @('First step', 'Second step') @('docs/notes.txt') }
        'order-touched-both' { Get-StepRunOrder @('First step', 'Second step') @('docs/notes.txt', 'src/code.cs') }
        'order-touched-none' { Get-StepRunOrder @('First step', 'Second step') @() }
        'order-from-git' { Get-StepRunOrder @('First step', 'Second step') }
    }
    Write-Host "RESULT $($order -join '|')"
    exit 0
}
switch ($Case) {
    'overlapping-inputs' {
        $script:gateStepInputs = [ordered]@{ 'Restore' = @('*'); 'Restore local tools' = @('dotnet-tools.json') }
        Write-Host "PATTERNS $(Get-StepInputPatterns 'Restore local tools (dotnet tool restore)')"
    }
    'exit' { $global:LASTEXITCODE = 3; $null = Test-Exit 'First step' }
    'never' { $global:LASTEXITCODE = $null; $null = Test-Exit 'First step' }
    'pass' { $global:LASTEXITCODE = 0; $null = Test-Exit 'First step' }
    'gate-output-dir' { Write-Host "OUTPUT $(Get-GateOutputDirectory 'Probe')" }
    'registry' { $global:LASTEXITCODE = 0; $null = Test-Exit 'First step'; $global:LASTEXITCODE = 1; $null = Test-Exit 'Second step' }
    'land-after-start' {
        Register-GateSteps @('First step')
        Register-StepInputs @{ 'First step' = @('*') }
        Initialize-GateState 'repo' $repo (Join-Path $Root 'state-land')
        Write-ProbeLanding ([DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds() + 60000)
        if (Test-StepCarried 'First step') { Write-Host 'RESULT carried' }
        else { $global:LASTEXITCODE = 0; $null = Test-Exit 'First step'; Write-Host 'RESULT ran' }
        Complete-Gate
    }
    'land-after-start-triage' {
        $env:GATE_RUN_KIND = 'triage'
        Register-GateSteps @('First step')
        Register-StepInputs @{ 'First step' = @('*') }
        Initialize-GateState 'repo' $repo (Join-Path $Root 'state-land-triage')
        Write-Host "LANDING-FILE $([string]::IsNullOrWhiteSpace($script:gateLandingFile))"
        Write-ProbeLanding ([DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds() + 60000)
        if (Test-StepCarried 'First step') { Write-Host 'RESULT carried' }
        else { $global:LASTEXITCODE = 0; $null = Test-Exit 'First step'; Write-Host 'RESULT ran' }
        Complete-Gate
    }
    'land-mid-step' {
        Register-GateSteps @('First step')
        Register-StepInputs @{ 'First step' = @('*') }
        Initialize-GateState 'repo' $repo (Join-Path $Root 'state-land')
        $null = Test-StepCarried 'First step'
        Write-ProbeLanding ([DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds() + 60000)
        $global:LASTEXITCODE = 0
        $null = Test-Exit 'First step'
        Complete-Gate
    }
    'land-before-start' {
        Register-GateSteps @('First step')
        Register-StepInputs @{ 'First step' = @('*') }
        Initialize-GateState 'repo' $repo (Join-Path $Root 'state-land')
        Write-ProbeLanding ($script:gateStartedAtMs - 60000)
        if (Test-StepCarried 'First step') { Write-Host 'RESULT carried' }
        else { $global:LASTEXITCODE = 0; $null = Test-Exit 'First step'; Write-Host 'RESULT ran' }
        Complete-Gate
    }
    'held-slot' { Write-Host "SLOT-AFTER holder=$([bool](Get-GateSlotHolder))" }
    'run-pass' {
        if (Test-StepCarried 'First step') { Write-Host 'RESULT carried' }
        else { $global:LASTEXITCODE = 0; $null = Test-Exit 'First step'; Write-Host 'RESULT ran' }
    }
    'run-pass-complete' {
        if (Test-StepCarried 'First step') { Write-Host 'RESULT carried' }
        else { $global:LASTEXITCODE = 0; $null = Test-Exit 'First step'; Write-Host 'RESULT ran' }
        Complete-Gate
    }
    'run-volatile-complete' {
        if (Test-StepCarried 'First step') { Write-Host 'RESULT carried' }
        else { $global:LASTEXITCODE = 0; $null = Test-Exit 'First step'; Write-Host 'RESULT ran' }
        Complete-Gate
    }
    'run-pass-drift' {
        if (Test-StepCarried 'First step') { Write-Host 'RESULT carried' }
        else { $global:LASTEXITCODE = 0; $null = Test-Exit 'First step'; Write-Host 'RESULT ran' }
        Add-Content -Path (Join-Path $repo 'code.cs') -Value 'class Drift {}'
        Complete-Gate
    }
    'run-pass-writes-output' {
        if (Test-StepCarried 'First step') { Write-Host 'RESULT carried' }
        else { $global:LASTEXITCODE = 0; $null = Test-Exit 'First step'; Write-Host 'RESULT ran' }
        $report = Join-Path $repo "App/StrykerOutput/$([guid]::NewGuid())/report.json"
        New-Item -ItemType Directory -Force -Path (Split-Path $report) | Out-Null
        Set-Content -Path $report -Value '{}'
        Complete-Gate
    }
    'selected-exact' { Test-SelectedTrx (Join-Path $fixtures 'passed.trx') 0 1 }
    'selected-fewer' { Test-SelectedTrx (Join-Path $fixtures 'passed.trx') 0 2 }
    'selected-more' { Test-SelectedTrx (Join-Path $fixtures 'passed-two.trx') 0 1 }
    'selected-failed' { Test-SelectedTrx (Join-Path $fixtures 'failed.trx') 1 1 }
    'selected-none' { Test-SelectedTrx (Join-Path $fixtures 'absent.trx') 8 1 }
    'selected-empty' { Invoke-SelectedTests $Root 'Probe.Tests' ' , ' $null }
    'selected-wildcard' { Invoke-SelectedTests $Root 'Probe.Tests' 'Widget_*' $null }
    'selected-no-project' { Invoke-SelectedTests $Root 'Absent.Tests' 'Widget_Renders' $null }
    'run-dependency' {
        Invoke-DependencyOrderStep
        Complete-Gate
    }
    'run-catalog' {
        Invoke-CatalogSteps
        Complete-Gate
    }
    { $_ -in 'run-origin', 'run-origin-triage', 'run-origin-unknown', 'run-origin-session' } {
        Invoke-OriginCurrencyStep
        Complete-Gate
    }
    'run-step-inputs' { Write-Host "RESULT $(Invoke-ProbeStep 'First step')" }
    'run-step-inputs-drift-outside' {
        Write-Host "RESULT $(Invoke-ProbeStep 'First step')"
        Add-Content -Path (Join-Path $repo 'docs/notes.txt') -Value 'drift outside the step inputs'
        Complete-Gate
    }
    'run-step-inputs-drift-inside' {
        Write-Host "RESULT $(Invoke-ProbeStep 'First step')"
        Add-Content -Path (Join-Path $repo 'src/code.cs') -Value 'class DriftInside {}'
        Complete-Gate
    }
    'run-tools' {
        $first = Invoke-ProbeStep 'First step'
        $catalog = Invoke-ProbeStep 'Code-style catalog'
        Write-Host "RESULT first=$first catalog=$catalog"
    }
    'run-fail' {
        if (Test-StepCarried 'First step') { Write-Host 'RESULT carried' }
        else { Write-Host 'RESULT ran'; $global:LASTEXITCODE = 1; $null = Test-Exit 'First step' }
    }
}
Write-Host 'REACHED'
Exit-GateSlot -Slot $script:gateSlot
exit 0

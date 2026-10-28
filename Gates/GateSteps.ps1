$script:gateDeclaredSteps = @()
$script:gateStepInputs = @{}
$script:gateVolatileSteps = @()
$script:gateProbeSteps = $null
$script:gateOriginStep = 'Current with origin'
$script:gateDependencyOrderStep = 'Dependency order'

function Register-GateSteps([string[]]$steps) {
    $script:gateDeclaredSteps = $steps
}

function Register-VolatileSteps([string[]]$steps) {
    $script:gateVolatileSteps = $steps
}

function Test-VolatileStep([string]$name) {
    [bool](Get-LongestStepPrefix $name @($script:gateVolatileSteps))
}

function Register-StepInputs([hashtable]$inputs) {
    foreach ($name in $inputs.Keys) { $script:gateStepInputs[$name] = [string[]]@($inputs[$name]) }
}

function Get-LongestStepPrefix([string]$name, [string[]]$candidates) {
    @($candidates | Where-Object { $name.StartsWith($_, [StringComparison]::Ordinal) } | Sort-Object Length -Descending)[0]
}

function Get-StepInputPatterns([string]$name) {
    $declared = Get-LongestStepPrefix $name @($script:gateStepInputs.Keys)
    if ($declared) { return $script:gateStepInputs[$declared] }
    return @('*')
}

function Test-ProbedStep([string]$name) {
    @($script:gateProbeSteps | Where-Object { $name.StartsWith($_, [StringComparison]::Ordinal) }).Count -gt 0
}

function Get-DiffTouchedFiles([string]$repoPath = $script:gateRepoPath) {
    <#
    .SYNOPSIS
        The repo-relative files the session's change touches: what HEAD holds beyond origin's default branch, plus
        every modified or untracked file. A missing origin/HEAD leaves only the working-tree part.
    #>
    $files = [Collections.Generic.HashSet[string]]::new([StringComparer]::OrdinalIgnoreCase)
    $global:LASTEXITCODE = $null
    $default = git -C $repoPath symbolic-ref --quiet --short refs/remotes/origin/HEAD 2>$null
    if ($global:LASTEXITCODE -eq 0 -and -not [string]::IsNullOrWhiteSpace($default)) {
        foreach ($line in @(git -C $repoPath diff --name-only "$default...HEAD" 2>$null)) {
            if (-not [string]::IsNullOrWhiteSpace($line)) { [void]$files.Add($line.Trim()) }
        }
    }
    foreach ($line in @(git -C $repoPath status --porcelain --untracked-files=all 2>$null)) {
        if ($line.Length -gt 3) { [void]$files.Add((($line.Substring(3) -split ' -> ')[-1]).Trim().Trim('"')) }
    }
    return @($files)
}

function Get-StepRunOrder([string[]]$steps, [string[]]$touched = (Get-DiffTouchedFiles)) {
    <#
    .SYNOPSIS
        The steps whose declared inputs the session's change touches first, in their registered order, then the
        rest, so a fail-fast on a step the session did not touch still leaves its own steps judged and recorded.
        For a gate whose steps are independent harnesses; a gate whose steps depend on one another keeps its order.
    #>
    $first = @($steps | Where-Object {
        $patterns = Get-StepInputPatterns $_
        @($touched | Where-Object { $file = $_; @($patterns | Where-Object { $file -like $_ }).Count -gt 0 }).Count -gt 0
    })
    $rest = @($steps | Where-Object { $_ -cnotin $first })
    if ($first.Count -gt 0) { Write-Host "Diff-touched steps first: $($first -join ', ')" }
    return @($first) + @($rest)
}

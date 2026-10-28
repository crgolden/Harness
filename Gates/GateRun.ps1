$script:failures = 0
$script:gateReportedSteps = @()

function Assert-RequestedSteps([AllowNull()][string[]]$Steps) {
    $passable = @($script:gateCatalogToolFiles.Keys) + @($script:gateDeclaredSteps)
    $usage = "pass the steps to run as -Steps '<step>,<step>' (every step to run all of them): $($passable -join ', ')"
    if ($null -eq $Steps -or @($Steps).Count -eq 0) { Stop-Gate 'Requested steps' "no -Steps given; $usage" }
    $requested = @($Steps | ForEach-Object { $_ -split ',' } | ForEach-Object { $_.Trim() } | Where-Object { $_ })
    $problems = [Collections.Generic.List[string]]::new()
    foreach ($name in $requested) {
        if ($name -cnotin $passable) { $problems.Add("'$name' is not a step of this gate") }
    }
    foreach ($step in $passable) {
        $passedAt = Get-StepCarryEvidence $step $passable
        if ($step -cin $requested -and $passedAt) {
            $problems.Add("'$step' does not qualify: it passed $passedAt on exactly these inputs, so it would return the same green")
        }
        if ($step -cnotin $requested -and -not $passedAt) {
            $problems.Add("'$step' must be passed: its inputs changed since its last pass, it last failed, or it never passed")
        }
    }
    if ($problems.Count -gt 0) { Stop-Gate 'Requested steps' "$($problems -join '; '); $usage" }
    Write-Row 'Requested steps' 'PASS' "running $($requested -join ', '); every other step carries a pass on its current inputs"
}

function Assert-StepInputsDeclared {
    if ($script:gateDeclaredSteps.Count -eq 0) { return }
    $problems = [Collections.Generic.List[string]]::new()
    foreach ($step in $script:gateDeclaredSteps) {
        if (-not $script:gateStepInputs.ContainsKey($step)) { $problems.Add("'$step' declares no inputs") }
    }
    foreach ($name in @($script:gateVolatileSteps)) {
        if ($name -cnotin $script:gateDeclaredSteps) { $problems.Add("'$name' is declared volatile but is not a registered step, so the step it meant would carry") }
    }
    foreach ($name in $script:gateStepInputs.Keys) {
        if ($name -cnotin $script:gateDeclaredSteps) { $problems.Add("'$name' declares inputs but is not a registered step") }
        foreach ($pattern in $script:gateStepInputs[$name]) {
            if ($pattern -eq '*') { continue }
            if (@($script:gateManifest.Keys | Where-Object { $_ -like $pattern }).Count -eq 0) {
                $problems.Add("'$name' input '$pattern' matches no file the gate fingerprints")
            }
        }
    }
    if ($problems.Count -gt 0) { Stop-Gate 'Step inputs declared' ($problems -join '; ') }
    Write-Row 'Step inputs declared' 'PASS' "$($script:gateDeclaredSteps.Count) steps, every pattern matches a file"
}

function Test-StepCarried([string]$name) {
    if ($null -ne $script:gateProbeSteps) {
        $script:gateReportedSteps += $name
        if (Test-ProbedStep $name) {
            $script:gateCurrentStep = $name
            $script:gateCurrentStepStartedAt = (Get-Date).ToUniversalTime().ToString('o')
            return $false
        }
        Write-Host ("{0,-60} {1,-8} {2}" -f $name, 'SKIPPED', 'not probed')
        return $true
    }
    $landing = Get-GateSupersedingLanding
    if ($landing) { Stop-SupersededGate $landing }
    if (Test-VolatileStep $name) { return $false }
    $previous = Get-CarriedVerdict $name
    if ($previous) {
        $script:gateReportedSteps += $name
        $where = if ($previous['from']) { " in checkout $($previous['from'])" } else { '' }
        if ($previous['from'] -and $script:gateVerdictsFile) {
            $copy = [ordered]@{}
            foreach ($key in @($previous.Keys)) { if ($key -cne 'from') { $copy[$key] = $previous[$key] } }
            $script:gateVerdicts[$name] = $copy
            Write-GateStateFile $script:gateVerdictsFile ($script:gateVerdicts | ConvertTo-Json -Depth 4)
        }
        Add-GateRunRow $name 'CARRIED' "passed $($previous.at)$where on these exact inputs"
        Write-Host ("{0,-60} {1,-8} {2}" -f $name, 'CARRIED', "passed $($previous.at)$where on these exact inputs")
        return $true
    }
    $script:gateCurrentStep = $name
    $script:gateCurrentStepStartedAt = (Get-Date).ToUniversalTime().ToString('o')
    Save-GateRun 'RUNNING'
    return $false
}

function Write-Row([string]$name, [string]$verdict, [string]$detail) {
    $script:gateReportedSteps += $name
    $seconds = Get-StepSeconds
    Add-GateRunRow $name $verdict $detail $seconds
    if ($verdict -in 'PASS', 'FAIL') { Save-StepVerdict $name $verdict }
    $script:gateUsedModules.Clear()
    $script:gateCurrentStep = $null
    $script:gateCurrentStepStartedAt = $null
    if ($script:gateStartedAt) { Save-GateRun 'RUNNING' }
    $timing = if ($null -ne $seconds) { " ({0}s)" -f $seconds } else { '' }
    Write-Host ("{0,-60} {1,-8} {2}{3}" -f $name, $verdict, $detail, $timing)
}

function Write-UnreachedSteps {
    if ($null -ne $script:gateProbeSteps) { return }
    foreach ($step in $script:gateDeclaredSteps) {
        if (-not ($script:gateReportedSteps | Where-Object { $_.StartsWith($step, [StringComparison]::Ordinal) })) {
            Add-GateRunRow $step 'NOT REACHED' 'fail-fast: an earlier step failed'
            Write-Host ("{0,-60} {1,-8} {2}" -f $step, 'NOT REACHED', 'fail-fast: an earlier step failed')
        }
    }
}

function Stop-Gate([string]$name, [string]$detail) {
    Write-Row $name 'FAIL' $detail
    $script:failures++
    Write-UnreachedSteps
    Complete-Gate
}

function Get-GateOutputDirectory([string]$repo) {
    $output = Join-Path (Get-GateSlotDirectory) $repo
    New-Item -ItemType Directory -Force -Path $output | Out-Null
    return $output
}

function Complete-Gate {
    if ($null -ne $script:gateProbeSteps) {
        Exit-GateSlot -Slot $script:gateSlot
        $script:gateSlot = $null
        if ($script:failures -eq 0) { Write-Host 'PROBE: PASSED; nothing was recorded, so the gate still owes every step'; exit 0 }
        Write-Host "PROBE: FAILED ($script:failures failing step(s)); nothing was recorded"
        exit 1
    }
    $landing = Get-GateSupersedingLanding
    if ($landing) { Stop-SupersededGate $landing }
    $drifted = Revoke-DriftedVerdicts
    Save-GateRun $(if ($drifted) { 'DRIFTED' } elseif ($script:failures -eq 0) { 'PASSED' } else { 'FAILED' })
    Exit-GateSlot -Slot $script:gateSlot
    $script:gateSlot = $null
    if ($script:failures -eq 0) { Write-Host 'GATE: PASSED'; exit 0 }
    Write-Host "GATE: FAILED ($script:failures failing step(s))"
    exit 1
}

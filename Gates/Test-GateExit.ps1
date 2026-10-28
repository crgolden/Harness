# GATE-STEP: Gate exit checker harness
<#
.SYNOPSIS
    Harness for GateExit.ps1 alone, against the bookkeeping double: Test-Exit passes a zero exit code, stops the
    gate on any other or on a command that never ran, and records its module for the step's fingerprint.
#>
. (Join-Path $PSScriptRoot 'gate-check-double.ps1')
. (Join-Path $PSScriptRoot 'GateExit.ps1')

$global:LASTEXITCODE = 3
$result = Invoke-Check { Test-Exit 'First step' }
Assert-Check 'must-block: a nonzero exit stops the gate naming the code' ($result.Stopped -and $result.Rows[0].Detail -ceq 'exit 3') (Format-Check $result)

$global:LASTEXITCODE = $null
$result = Invoke-Check { Test-Exit 'First step' }
Assert-Check 'must-block: a command that never ran stops the gate' ($result.Stopped -and $result.Rows[0].Detail -ceq 'command never ran') (Format-Check $result)

$global:LASTEXITCODE = 0
$result = Invoke-Check { Test-Exit 'First step' }
Assert-Check 'control: a zero exit passes and lets the gate go on' (-not $result.Stopped -and $result.Rows[0].Verdict -ceq 'PASS' -and $result.Value -eq $true) (Format-Check $result)
Assert-Check 'must-pass: Test-Exit records its own module, so a GateExit.ps1 edit owes the steps that called it' ($result.Modules -ccontains 'Gates/GateExit.ps1') ($result.Modules -join ', ')

Complete-CheckHarness

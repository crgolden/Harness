# GATE-STEP: Gate TRX checker harness
<#
.SYNOPSIS
    Harness for GateTrx.ps1 alone, against the bookkeeping double: Test-Trx passes only a TRX whose run executed at
    least the floor with nothing failed or skipped and a zero exit, and stops the gate naming every failed test.
#>
. (Join-Path $PSScriptRoot 'gate-check-double.ps1')
. (Join-Path $PSScriptRoot 'GateTrx.ps1')

$work = New-ClaudeTempPath "gate-trx-test-$PID"
New-Item -ItemType Directory -Force -Path $work | Out-Null
try {
    $fixtures = @{
        'passed.trx'  = '<TestRun><ResultSummary><Counters total="1" executed="1" passed="1" failed="0" /></ResultSummary></TestRun>'
        'failed.trx'  = '<TestRun><Results><UnitTestResult testName="Planted.Fails" outcome="Failed"><Output><ErrorInfo><Message>planted failure message</Message></ErrorInfo></Output></UnitTestResult></Results><ResultSummary><Counters total="1" executed="1" passed="0" failed="1" /></ResultSummary></TestRun>'
        'skipped.trx' = '<TestRun><ResultSummary><Counters total="2" executed="1" passed="1" failed="0" notExecuted="1" /></ResultSummary></TestRun>'
    }
    foreach ($name in $fixtures.Keys) { Set-Content -LiteralPath (Join-Path $work $name) -Value $fixtures[$name] -Encoding utf8 }

    $result = Invoke-Check { Test-Trx 'First step' (Join-Path $work 'absent.trx') 0 1 }
    Assert-Check 'must-block: a missing TRX stops the gate' ($result.Stopped -and $result.Rows[0].Detail -ceq 'no TRX (exit 0)') (Format-Check $result)
    $result = Invoke-Check { Test-Trx 'First step' (Join-Path $work 'failed.trx') 1 1 }
    Assert-Check 'must-block: a failed test stops the gate and is named with its message' ($result.Stopped -and $result.Rows[0].Detail -like '*failed 1*' -and $result.Printed -like '*Planted.Fails: planted failure message*') (Format-Check $result)
    $result = Invoke-Check { Test-Trx 'First step' (Join-Path $work 'skipped.trx') 0 1 }
    Assert-Check 'must-block: a test not executed stops the gate' ($result.Stopped -and $result.Rows[0].Detail -like '*not executed 1*') (Format-Check $result)
    $result = Invoke-Check { Test-Trx 'First step' (Join-Path $work 'passed.trx') 0 2 }
    Assert-Check 'must-block: fewer tests executed than the floor stops the gate' ($result.Stopped -and $result.Rows[0].Detail -like 'executed 1 (floor 2)*') (Format-Check $result)
    $result = Invoke-Check { Test-Trx 'First step' (Join-Path $work 'passed.trx') 3 1 }
    Assert-Check 'must-block: a nonzero exit stops the gate even when every test passed' ($result.Stopped -and $result.Rows[0].Detail -like '*exit 3') (Format-Check $result)
    $result = Invoke-Check { Test-Trx 'First step' (Join-Path $work 'passed.trx') 0 1 }
    Assert-Check 'control: a TRX meeting its floor with nothing failed passes' (-not $result.Stopped -and $result.Rows[0].Verdict -ceq 'PASS' -and $result.Rows[0].Detail -like 'executed 1 (floor 1), failed 0*') (Format-Check $result)
    Assert-Check 'must-pass: Test-Trx records its own module' ($result.Modules -ccontains 'Gates/GateTrx.ps1') ($result.Modules -join ', ')
} finally {
    Remove-FixtureTree $work
}
Complete-CheckHarness

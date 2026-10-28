# GATE-STEP: Gate SARIF checker harness
<#
.SYNOPSIS
    Harness for GateSarif.ps1 alone, against the bookkeeping double: Test-Sarif passes a report with no results,
    stops the gate on a missing report or on any result (each listed), and New-InspectCodeCaches always hands back
    an empty cache folder.
#>
. (Join-Path $PSScriptRoot 'gate-check-double.ps1')
. (Join-Path $PSScriptRoot 'GateSarif.ps1')

$work = New-ClaudeTempPath "gate-sarif-test-$PID"
New-Item -ItemType Directory -Force -Path $work | Out-Null
try {
    Set-Content -LiteralPath (Join-Path $work 'clean.sarif') -Value '{"runs":[{"results":[]}]}' -Encoding utf8
    Set-Content -LiteralPath (Join-Path $work 'dirty.sarif') -Encoding utf8 -Value ('{"runs":[{"results":[{"ruleId":"PlantedRule","message":{"text":"planted finding"},' +
        '"locations":[{"physicalLocation":{"artifactLocation":{"uri":"Planted/File.cs"},"region":{"startLine":7}}}]}]}]}')

    $result = Invoke-Check { Test-Sarif (Join-Path $work 'absent.sarif') }
    Assert-Check 'must-block: a missing SARIF report stops the gate' ($result.Stopped -and $result.Rows[0].Detail -ceq 'no SARIF report written') (Format-Check $result)
    $result = Invoke-Check { Test-Sarif (Join-Path $work 'dirty.sarif') }
    Assert-Check 'must-block: a SARIF result stops the gate and is listed with its rule, file and line' ($result.Stopped -and $result.Rows[0].Detail -like '1 results*' -and $result.Printed -like '*PlantedRule Planted/File.cs:7 planted finding*') (Format-Check $result)
    $result = Invoke-Check { Test-Sarif (Join-Path $work 'clean.sarif') }
    Assert-Check 'control: a report with no results passes' (-not $result.Stopped -and $result.Rows[0].Verdict -ceq 'PASS' -and $result.Rows[0].Detail -ceq '0 results') (Format-Check $result)
    Assert-Check 'must-pass: Test-Sarif records its own module' ($result.Modules -ccontains 'Gates/GateSarif.ps1') ($result.Modules -join ', ')

    $stale = Join-Path $work 'gate-output\jb-caches\stale.bin'
    New-Item -ItemType Directory -Force -Path (Split-Path $stale) | Out-Null
    Set-Content -LiteralPath $stale -Value 'stale'
    $caches = New-InspectCodeCaches (Join-Path $work 'gate-output')
    Assert-Check 'must-block: a cache folder left by an earlier run is handed back empty' ((Test-Path -LiteralPath $caches) -and @(Get-ChildItem -Force -LiteralPath $caches).Count -eq 0)
} finally {
    Remove-FixtureTree $work
}
Complete-CheckHarness

# GATE-STEP: Gate Sonar checker harness
<#
.SYNOPSIS
    Harness for GateSonar.ps1 alone, against the bookkeeping double: the open-issue check passes only a fresh
    analysis of the named branch holding zero issues and fails, listing every issue, on open issues, a stale or
    absent analysis, a missing total and an unreachable server; the branch is branch-local-<machine> with
    GATE_RUN_KIND unset, branch-triage-<machine> under triage, and any other run kind fails the gate; JAVA_HOME is the
    jre beside the sonar-scanner on PATH, or nothing.
#>
. (Join-Path $PSScriptRoot 'gate-check-double.ps1')
. (Join-Path $PSScriptRoot 'GateSonar.ps1')

$work = New-ClaudeTempPath "gate-sonar-test-$PID"
New-Item -ItemType Directory -Force -Path $work | Out-Null
$savedPath = $env:PATH
$savedKind = $env:GATE_RUN_KIND
try {
    $branches = '{"branches":[{"name":"branch-local-probe","analysisDate":"2026-01-15T10:00:00+0000"}]}'
    $clean = '{"total":0,"issues":[]}'
    $issues = '{"total":2,"issues":[{"rule":"csharpsquid:S1","component":"crgolden_Probe:Planted/File.cs","line":7,"message":"planted smell"},' +
        '{"rule":"typescript:S2","component":"crgolden_Probe:planted/file.ts","line":3,"message":"second smell"}]}'
    $before = [DateTimeOffset]::Parse('2026-01-15T09:00:00Z')

    $result = Invoke-Check { Test-SonarIssueReport 'First step' $branches $clean 'branch-local-probe' $before }
    Assert-Check 'control: a fresh analysis of the branch with zero issues passes' (-not $result.Stopped -and $result.Rows[0].Detail -like '0 open issues on branch-local-probe*') (Format-Check $result)
    Assert-Check 'must-pass: the issue check records its own module' ($result.Modules -ccontains 'Gates/GateSonar.ps1') ($result.Modules -join ', ')
    $result = Invoke-Check { Test-SonarIssueReport 'First step' $branches $issues 'branch-local-probe' $before }
    Assert-Check 'must-block: open issues stop the gate, each listed by rule, file and line' ($result.Stopped -and $result.Rows[0].Detail -like '2 open issue(s)*' -and
        $result.Printed -like '*csharpsquid:S1 Planted/File.cs:7 planted smell*' -and $result.Printed -like '*typescript:S2 planted/file.ts:3 second smell*') (Format-Check $result)
    $result = Invoke-Check { Test-SonarIssueReport 'First step' $branches $clean 'branch-local-probe' ([DateTimeOffset]::Parse('2026-01-15T11:00:00Z')) }
    Assert-Check 'must-block: an analysis older than this run stops the gate' ($result.Stopped -and $result.Rows[0].Detail -like '*predates this run*') (Format-Check $result)
    $result = Invoke-Check { Test-SonarIssueReport 'First step' $branches $clean 'branch-local-absent' $before }
    Assert-Check 'must-block: no analysis of the named branch stops the gate' ($result.Stopped -and $result.Rows[0].Detail -like '*holds no analysis of branch branch-local-absent*') (Format-Check $result)
    $result = Invoke-Check { Test-SonarIssueReport 'First step' $branches '{"issues":[]}' 'branch-local-probe' $before }
    Assert-Check 'must-block: an issue search with no total stops the gate' ($result.Stopped -and $result.Rows[0].Detail -ceq 'the issue search returned no total') (Format-Check $result)
    $result = Invoke-Check { Test-SonarIssues 'First step' 'crgolden_Probe' 'branch-local-probe' ([DateTimeOffset]::UtcNow) 'http://127.0.0.1:9' }
    Assert-Check 'must-block: an unreachable server stops the gate' ($result.Stopped -and $result.Rows[0].Detail -like 'the SonarCloud query failed*') (Format-Check $result)

    $machine = $env:COMPUTERNAME.ToLowerInvariant()
    $env:GATE_RUN_KIND = $null
    Assert-Check 'an operator run analyzes onto branch-local-<machine>' ((Get-SonarBranchName) -ceq "branch-local-$machine")
    $env:GATE_RUN_KIND = 'triage'
    Assert-Check 'a triage run analyzes onto branch-triage-<machine>' ((Get-SonarBranchName) -ceq "branch-triage-$machine")
    $child = Join-Path $work 'branch-unknown.ps1'
    Set-Content -LiteralPath $child -Encoding utf8 -Value @('param([string]$Module)', '. $Module', "`$env:GATE_RUN_KIND = 'Triage'", 'Get-SonarBranchName')
    $printed = (& pwsh -NoProfile -File $child -Module (Join-Path $PSScriptRoot 'GateSonar.ps1') 2>&1 | ForEach-Object { "$_" }) -join "`n"
    Assert-Check 'must-block: any other run kind fails the gate rather than analyze onto the operator''s branch' ($LASTEXITCODE -eq 1 -and $printed -like "*GATE: FAILED (GATE_RUN_KIND 'Triage' is neither unset nor triage)*" -and $printed -notlike '*branch-*') $printed

    $scannerHome = Join-Path $work 'scanner\sonar-scanner-9.9.9.9-windows-x64'
    New-Item -ItemType Directory -Force -Path (Join-Path $scannerHome 'bin') | Out-Null
    Set-Content -LiteralPath (Join-Path $scannerHome 'bin\sonar-scanner.bat') -Value '@echo off'
    $env:PATH = Join-Path $work 'empty'
    Assert-Check 'must-not-find: no sonar-scanner on PATH yields none' ($null -eq (Find-SonarScannerJavaHome))
    $env:PATH = Join-Path $scannerHome 'bin'
    Assert-Check 'must-not-find: a scanner with no jre beside it yields none' ($null -eq (Find-SonarScannerJavaHome))
    New-Item -ItemType Directory -Force -Path (Join-Path $scannerHome 'jre\bin') | Out-Null
    Set-Content -LiteralPath (Join-Path $scannerHome 'jre\bin\java.exe') -Value 'planted'
    $found = Find-SonarScannerJavaHome
    Assert-Check 'the jre beside the scanner on PATH is found, whatever version names its folder' ($found -and $found.EndsWith('sonar-scanner-9.9.9.9-windows-x64\jre')) $found
} finally {
    $env:PATH = $savedPath
    $env:GATE_RUN_KIND = $savedKind
    Remove-FixtureTree $work
}
Complete-CheckHarness

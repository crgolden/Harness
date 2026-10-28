function Stop-SelectedTests([string]$detail) {
    Write-Host "TESTS: FAILED ($detail)"
    Exit-GateSlot -Slot $script:selectedTestsSlot
    exit 1
}

function Test-SelectedTrx([string]$trx, [int]$exit, [int]$requested) {
    if (-not (Test-Path $trx)) { Stop-SelectedTests "no TRX (exit $exit)" }
    [xml]$doc = Get-Content $trx -Raw
    $c = $doc.TestRun.ResultSummary.Counters
    $detail = "executed $($c.executed) of $requested requested, failed $($c.failed), not executed $($c.notExecuted), exit $exit"
    if ($exit -ne 0 -or [int]$c.failed -ne 0 -or [int]$c.notExecuted -ne 0 -or [int]$c.executed -ne $requested) {
        $doc.TestRun.Results.UnitTestResult | Where-Object { $_.outcome -ne 'Passed' } |
            ForEach-Object { Write-Host ("    {0}: {1}" -f $_.testName, $_.Output.ErrorInfo.Message) }
        if ([int]$c.executed -gt $requested) { Write-Host '    a bare name matched a same-named method in another class; pass it fully qualified' }
        Stop-SelectedTests $detail
    }
    $doc.TestRun.Results.UnitTestResult | ForEach-Object { Write-Host ("    {0}: {1}" -f $_.testName, $_.outcome) }
    Write-Host "TESTS: PASSED ($detail)"
    Exit-GateSlot -Slot $script:selectedTestsSlot
    exit 0
}

function Invoke-SelectedTests([string]$repoPath, [string]$project, [string]$tests, [scriptblock]$setup) {
    $names = @($tests -split ',' | ForEach-Object { $_.Trim() } | Where-Object { $_ })
    if ($names.Count -eq 0) { Stop-SelectedTests 'no test names given' }
    $invalid = @($names | Where-Object { $_ -notmatch '^[A-Za-z_][\w.+]*$' })
    if ($invalid.Count -gt 0) { Stop-SelectedTests "not a test method name: $($invalid -join ', ')" }
    if ([string]::IsNullOrWhiteSpace($project)) { Stop-SelectedTests '-Tests needs -TestProject' }
    $projectPath = Join-Path $repoPath $project
    if (-not (Test-Path -LiteralPath $projectPath -PathType Container)) { Stop-SelectedTests "no test project at $projectPath" }
    $script:selectedTestsSlot = Enter-GateSlotForGate (Split-Path -Leaf $repoPath) 'TESTS: FAILED'
    Write-Host "TESTS-ONLY RUN of $($names.Count) test(s) in ${project}: not a gate; it writes no gate record and carries no verdict"
    $results = Join-Path (Get-GateSlotDirectory) "$(Split-Path -Leaf $repoPath)\selected-tests"
    New-Item -ItemType Directory -Force -Path $results | Out-Null
    $trx = Join-Path $results 'selected-tests.trx'
    if (Test-Path $trx) { Remove-Item $trx -Force }
    $global:LASTEXITCODE = $null
    dotnet build "$projectPath" --configuration Release /p:RestoreLockedMode=true -warnaserror
    if ($global:LASTEXITCODE -ne 0) { Stop-SelectedTests "build exit $global:LASTEXITCODE" }
    if ($setup) { & $setup }
    $filters = @($names | ForEach-Object { if ($_.Contains('.')) { $_ } else { "*.$_" } })
    $global:LASTEXITCODE = $null
    dotnet test --project "$projectPath" --no-build --configuration Release -- --filter-method @filters --report-xunit-trx --report-xunit-trx-filename selected-tests.trx --results-directory="$results"
    Test-SelectedTrx $trx $global:LASTEXITCODE $names.Count
}

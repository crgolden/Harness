function Test-Trx([string]$name, [string]$trx, [int]$exit, [int]$floor) {
    Use-GateModule 'Gates/GateTrx.ps1'
    if (-not (Test-Path $trx)) { Stop-Gate $name "no TRX (exit $exit)" }
    [xml]$doc = Get-Content $trx -Raw
    $c = $doc.TestRun.ResultSummary.Counters
    $detail = "executed $($c.executed) (floor $floor), failed $($c.failed), not executed $($c.notExecuted), exit $exit"
    if ($exit -ne 0 -or [int]$c.failed -ne 0 -or [int]$c.notExecuted -ne 0 -or [int]$c.executed -lt $floor) {
        $doc.TestRun.Results.UnitTestResult | Where-Object { $_.outcome -ne 'Passed' } |
            ForEach-Object { Write-Host ("    {0}: {1}" -f $_.testName, $_.Output.ErrorInfo.Message) }
        Stop-Gate $name $detail
    }
    Write-Row $name 'PASS' $detail
}

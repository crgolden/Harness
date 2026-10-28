function New-InspectCodeCaches([string]$gateOutput) {
    $caches = Join-Path $gateOutput 'jb-caches'
    if (Test-Path $caches) { Remove-Item $caches -Recurse -Force }
    New-Item -ItemType Directory -Path $caches | Out-Null
    return $caches
}

function Test-Sarif([string]$sarif) {
    Use-GateModule 'Gates/GateSarif.ps1'
    if (-not (Test-Path $sarif)) { Stop-Gate 'jb inspectcode' 'no SARIF report written' }
    try {
        $report = Get-Content $sarif -Raw | ConvertFrom-Json
        $results = @($report.runs | ForEach-Object { $_.results })
    }
    catch { Stop-Gate 'jb inspectcode' "unparsable SARIF: $_" }
    if ($results.Count -eq 0) { Write-Row 'jb inspectcode' 'PASS' '0 results'; return }
    $results | ForEach-Object {
        $loc = $_.locations[0].physicalLocation
        Write-Host ("    {0} {1}:{2} {3}" -f $_.ruleId, $loc.artifactLocation.uri, $loc.region.startLine, $_.message.text)
    }
    Stop-Gate 'jb inspectcode' "$($results.Count) results (listed above)"
}

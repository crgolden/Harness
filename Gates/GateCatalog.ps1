$script:gateCatalogToolFiles = @{
    'Code-style catalog' = @('Claude/Get-CodeStyleCatalog.ps1', 'Shared/ScanScopeHelper.ps1')
    'Comment catalog'    = @('CommentAudit/Get-CommentCatalog.ps1', 'Shared/ScanScopeHelper.ps1')
}

function Invoke-CatalogSteps {
    $catalogs = [ordered]@{
        'Code-style catalog' = Join-Path $script:gateToolsRoot 'Claude\Get-CodeStyleCatalog.ps1'
        'Comment catalog'    = Join-Path $script:gateToolsRoot 'CommentAudit\Get-CommentCatalog.ps1'
    }
    $script:gateDeclaredSteps = @($script:gateOriginStep, $script:gateDependencyOrderStep) + @($catalogs.Keys) + $script:gateDeclaredSteps
    if ($null -eq $script:gateProbeSteps -or (Test-ProbedStep $script:gateOriginStep)) { Invoke-OriginCurrencyStep }
    if ($null -eq $script:gateProbeSteps -or (Test-ProbedStep $script:gateDependencyOrderStep)) { Invoke-DependencyOrderStep }
    $repo = $script:gateRepoName
    $output = Get-GateOutputDirectory $repo
    foreach ($step in $catalogs.Keys) {
        if (Test-StepCarried $step) { continue }
        $csv = Join-Path $output (($step -replace '\W+', '-') + '.csv')
        if (Test-Path -LiteralPath $csv) { Remove-Item -LiteralPath $csv }
        $global:LASTEXITCODE = $null
        & pwsh -File $catalogs[$step] -RepoPath $script:gateRepoPath -Repo $repo -OutputPath $csv *> ($csv -replace '\.csv$', '.log')
        if ($global:LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath $csv)) { Stop-Gate $step "the catalog did not complete (exit $global:LASTEXITCODE)" }
        $rows = @(Import-Csv -LiteralPath $csv | Where-Object { $_.Disposition -notin 'keep', 'exempt' })
        if ($rows.Count -gt 0) {
            $rows | Select-Object -First 20 | ForEach-Object {
                Write-Host ("    {0}:{1} {2}/{3} {4}" -f $_.Path, $_.Line, $_.Category, $_.Disposition, ([string]$_.Snippet).Trim())
            }
            Stop-Gate $step "$($rows.Count) row(s), the first 20 listed above; the full CSV is $csv"
        }
        Write-Row $step 'PASS' '0 rows'
    }
}

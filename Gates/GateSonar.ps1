function Find-SonarScannerJavaHome {
    $scanner = Get-Command 'sonar-scanner' -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
    if (-not $scanner) { return $null }
    $javaHome = Join-Path (Split-Path -Parent (Split-Path -Parent $scanner.Source)) 'jre'
    if (Test-Path -LiteralPath (Join-Path $javaHome 'bin\java.exe')) { return $javaHome }
    return $null
}

function Get-SonarScannerJavaHome([string]$name) {
    Use-GateModule 'Gates/GateSonar.ps1'
    $javaHome = Find-SonarScannerJavaHome
    if (-not $javaHome) { Stop-Gate $name 'sonar-scanner is not on PATH with its bundled jre\bin\java.exe beside its bin folder' }
    return $javaHome
}

function Get-SonarBranchName {
    $machine = $env:COMPUTERNAME.ToLowerInvariant()
    if ([string]::IsNullOrWhiteSpace($env:GATE_RUN_KIND)) { return "branch-local-$machine" }
    if ([string]::Equals($env:GATE_RUN_KIND, 'triage', [StringComparison]::Ordinal)) { return "branch-triage-$machine" }
    Write-Host "GATE: FAILED (GATE_RUN_KIND '$env:GATE_RUN_KIND' is neither unset nor triage)"
    exit 1
}

function ConvertFrom-SonarDate([string]$text) {
    return [DateTimeOffset]::ParseExact(($text -replace '([+-]\d{2})(\d{2})$', '$1:$2'), "yyyy-MM-dd'T'HH:mm:sszzz",
        [Globalization.CultureInfo]::InvariantCulture)
}

function Test-SonarIssueReport([string]$name, [string]$branchesJson, [string]$issuesJson, [string]$branch, [DateTimeOffset]$since) {
    Use-GateModule 'Gates/GateSonar.ps1'
    try {
        $branches = $branchesJson | ConvertFrom-Json -DateKind String
        $report = $issuesJson | ConvertFrom-Json -DateKind String
    }
    catch { Stop-Gate $name "unparsable SonarCloud response: $_" }
    $analysis = @($branches.branches | Where-Object { $_.name -ceq $branch })
    if ($analysis.Count -ne 1 -or -not $analysis[0].analysisDate) { Stop-Gate $name "SonarCloud holds no analysis of branch $branch" }
    $analysedAt = ConvertFrom-SonarDate $analysis[0].analysisDate
    $floor = $since.AddTicks(-($since.Ticks % [TimeSpan]::TicksPerSecond))
    if ($analysedAt -lt $floor) { Stop-Gate $name "the latest analysis of $branch ($($analysedAt.ToString('o'))) predates this run ($($floor.ToString('o')))" }
    if ($null -eq $report.total) { Stop-Gate $name 'the issue search returned no total' }
    if ($report.total -eq 0) { Write-Row $name 'PASS' "0 open issues on $branch, analyzed $($analysedAt.ToString('o'))"; return }
    $report.issues | ForEach-Object {
        Write-Host ("    {0} {1}:{2} {3}" -f $_.rule, ($_.component -replace '^[^:]*:'), $_.line, $_.message)
    }
    Stop-Gate $name "$($report.total) open issue(s) on $branch (listed above)"
}

function Test-SonarIssues([string]$name, [string]$projectKey, [string]$branch, [DateTimeOffset]$since, [string]$server = 'https://sonarcloud.io') {
    Use-GateModule 'Gates/GateSonar.ps1'
    $auth = [Convert]::ToBase64String([Text.Encoding]::ASCII.GetBytes("$($env:SONAR_TOKEN):"))
    $headers = @{ Authorization = "Basic $auth" }
    $project = [Uri]::EscapeDataString($projectKey)
    try {
        $branches = (Invoke-WebRequest -Uri "$server/api/project_branches/list?project=$project" -Headers $headers).Content
        $issues = (Invoke-WebRequest -Headers $headers `
            -Uri "$server/api/issues/search?componentKeys=$project&branch=$([Uri]::EscapeDataString($branch))&issueStatuses=OPEN,CONFIRMED&ps=500").Content
    }
    catch { Stop-Gate $name "the SonarCloud query failed: $_" }
    Test-SonarIssueReport $name $branches $issues $branch $since
}

$probe = Join-Path $PSScriptRoot 'gate-common-probe.ps1'
$baseline = [Environment]::GetEnvironmentVariables()
while ($null -ne ($line = [Console]::In.ReadLine())) {
    $case, $root = $line -split "`t", 2
    foreach ($name in @([Environment]::GetEnvironmentVariables().Keys)) {
        if (-not $baseline.Contains($name)) { [Environment]::SetEnvironmentVariable($name, $null) }
    }
    foreach ($name in $baseline.Keys) { [Environment]::SetEnvironmentVariable($name, $baseline[$name]) }
    $runspace = [runspacefactory]::CreateRunspace()
    $runspace.Open()
    $shell = [powershell]::Create()
    $shell.Runspace = $runspace
    $null = $shell.AddScript("& '$($probe.Replace("'", "''"))' -Case '$($case.Replace("'", "''"))' -Root '$($root.Replace("'", "''"))' *>&1 | ForEach-Object { [string]`$_ }; 'EXIT=' + `$LASTEXITCODE")
    $lines = @(try { $shell.Invoke() } catch { "HOST ERROR: $($_.Exception.Message)"; 'EXIT=1' })
    $shell.Dispose()
    $runspace.Dispose()
    $exit = 0
    foreach ($output in $lines) {
        if ($output -like 'EXIT=*') { $code = $output.Substring(5); if ($code) { $exit = [int]$code } }
        else { [Console]::Out.WriteLine($output) }
    }
    [Console]::Out.WriteLine("@@PROBE-END $exit")
    [Console]::Out.Flush()
}

function Test-Exit([string]$name) {
    Use-GateModule 'Gates/GateExit.ps1'
    if ($null -eq $global:LASTEXITCODE) { Stop-Gate $name 'command never ran' }
    if ($global:LASTEXITCODE -ne 0) { Stop-Gate $name "exit $global:LASTEXITCODE" }
    Write-Row $name 'PASS' ''
    return $true
}

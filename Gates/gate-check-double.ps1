<#
.SYNOPSIS
    The test double a checker module's harness loads in place of the gate library's bookkeeping, so that harness
    dot-sources only the checker it tests and an edit to the library's carry, record or rows reruns none of them.
    Use-GateModule, Write-Row and Stop-Gate keep the library's signatures; Stop-Gate records its FAIL row and throws
    GateStopped instead of ending the process, which Invoke-Check catches.
#>
. (Join-Path $PSScriptRoot '..\Hooks\ClaudeTemp.ps1')

class GateStopped : Exception {
    GateStopped([string]$message) : base($message) {}
}

$script:checkRows = [Collections.Generic.List[object]]::new()
$script:checkModules = [Collections.Generic.List[string]]::new()
$script:checkFailures = 0

function Use-GateModule([string]$module) { $script:checkModules.Add($module) }

function Write-Row([string]$name, [string]$verdict, [string]$detail) {
    $script:checkRows.Add([pscustomobject]@{ Name = $name; Verdict = $verdict; Detail = $detail })
}

function Stop-Gate([string]$name, [string]$detail) {
    Write-Row $name 'FAIL' $detail
    throw [GateStopped]::new($detail)
}

function Invoke-Check([scriptblock]$Check) {
    <#
    .SYNOPSIS
        Runs one checker call and returns what it reported: its rows, whether it stopped the gate, the modules it
        recorded, its value and everything it printed.
    #>
    $script:checkRows.Clear()
    $script:checkModules.Clear()
    $stopped = $false
    $printed = [Collections.Generic.List[string]]::new()
    $value = $null
    try {
        $value = & $Check *>&1 | ForEach-Object {
            if ($_ -is [Management.Automation.InformationRecord] -or $_ -is [Management.Automation.ErrorRecord]) { $printed.Add("$_") }
            else { $_ }
        }
    } catch [GateStopped] {
        $stopped = $true
    }
    [pscustomobject]@{
        Rows    = @($script:checkRows)
        Stopped = $stopped
        Modules = @($script:checkModules)
        Value   = $value
        Printed = ($printed -join "`n")
    }
}

function Assert-Check([string]$Name, [bool]$Ok, [string]$Detail = '') {
    if ($Ok) { Write-Output "PASS  $Name" }
    else {
        $script:checkFailures++
        Write-Output "FAIL  $Name  got: $Detail"
    }
}

function Format-Check($Result) {
    "stopped=$($Result.Stopped) rows=$(@($Result.Rows | ForEach-Object { "$($_.Verdict):$($_.Detail)" }) -join ' | ') printed=$($Result.Printed)"
}

function Complete-CheckHarness {
    if ($script:checkFailures) { Write-Output "$script:checkFailures case(s) failed."; exit 1 }
    Write-Output 'All cases passed.'
    exit 0
}

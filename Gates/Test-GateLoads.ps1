# GATE-STEP: Gate module loads harness
# GATE-LISTING: GetFiles($PSScriptRoot | Gates/*.ps1
<#
.SYNOPSIS
    Each consumer of the gate library loads by literal path only the modules it uses, and no module loads another,
    so its load list is its dependency declaration and the gate plan reruns it only when one of those files changes.
    This harness proves the list complete: for GateCommon.ps1, the bookkeeping probe and every checker harness, every
    library function the loaded files call is defined in a file the consumer loads. Parse only; nothing is loaded.
#>
$failures = 0
function Assert-Load([string]$Name, [bool]$Ok, [string]$Detail = '') {
    if ($Ok) { Write-Output "PASS  $Name" } else { $script:failures++; Write-Output "FAIL  $Name  got: $Detail" }
}

$definitions = @{}
$parsed = @{}
foreach ($file in [IO.Directory]::GetFiles($PSScriptRoot, '*.ps1')) {
    $leaf = [IO.Path]::GetFileName($file)
    $parsed[$leaf] = [Management.Automation.Language.Parser]::ParseFile($file, [ref]$null, [ref]$null)
    foreach ($function in $parsed[$leaf].FindAll({ param($n) $n -is [Management.Automation.Language.FunctionDefinitionAst] }, $true)) {
        if (-not $definitions.ContainsKey($function.Name)) { $definitions[$function.Name] = [Collections.Generic.List[string]]::new() }
        $definitions[$function.Name].Add($leaf)
    }
}

function Get-DotSourced($Ast) {
    foreach ($command in $Ast.FindAll({ param($n) $n -is [Management.Automation.Language.CommandAst] -and $n.InvocationOperator -eq 'Dot' }, $true)) {
        foreach ($string in $command.FindAll({ param($n) $n -is [Management.Automation.Language.StringConstantExpressionAst] -or $n -is [Management.Automation.Language.ExpandableStringExpressionAst] }, $true)) {
            $match = [regex]::Match($string.Value, '(?:^|[\\/])([\w.-]+\.ps1)$')
            if ($match.Success) { $match.Groups[1].Value }
        }
    }
}

function Get-MissingCalls($Ast, [string]$Self) {
    <#
    .SYNOPSIS
        Each library function some file the consumer loads calls that no loaded file defines, as "<caller file>:
        <function>". The consumer's loads are followed transitively; a file outside Gates is never judged.
    #>
    $loaded = [Collections.Generic.List[string]]::new()
    $pending = [Collections.Generic.Queue[string]]::new()
    $pending.Enqueue($Self)
    $asts = @{ $Self = $Ast }
    while ($pending.Count) {
        $leaf = $pending.Dequeue()
        if ($loaded -ccontains $leaf) { continue }
        $loaded.Add($leaf)
        $tree = if ($asts.ContainsKey($leaf)) { $asts[$leaf] } else { $parsed[$leaf] }
        if (-not $tree) { continue }
        foreach ($next in Get-DotSourced $tree) { if ($parsed.ContainsKey($next)) { $pending.Enqueue($next) } }
    }
    $available = [Collections.Generic.HashSet[string]]::new([StringComparer]::OrdinalIgnoreCase)
    foreach ($leaf in $loaded) {
        $tree = if ($asts.ContainsKey($leaf)) { $asts[$leaf] } else { $parsed[$leaf] }
        foreach ($function in $tree.FindAll({ param($n) $n -is [Management.Automation.Language.FunctionDefinitionAst] }, $true)) { [void]$available.Add($function.Name) }
    }
    foreach ($leaf in $loaded) {
        $tree = if ($asts.ContainsKey($leaf)) { $asts[$leaf] } else { $parsed[$leaf] }
        foreach ($call in $tree.FindAll({ param($n) $n -is [Management.Automation.Language.CommandAst] }, $true)) {
            $name = $call.GetCommandName()
            if ($name -and $definitions.ContainsKey($name) -and -not $available.Contains($name)) { "${leaf}: $name" }
        }
    }
}

$consumers = @('GateCommon.ps1', 'gate-common-probe.ps1') + @($parsed.Keys | Where-Object { $_ -like 'Test-Gate*.ps1' -and $_ -cne 'Test-GateLoads.ps1' } | Sort-Object)
Assert-Load 'control: the checker harnesses are found, so the rows below judge something' (@($consumers | Where-Object { $_ -like 'Test-Gate*.ps1' }).Count -ge 5) ($consumers -join ', ')
foreach ($consumer in $consumers) {
    $missing = @(Get-MissingCalls $parsed[$consumer] $consumer | Sort-Object -Unique)
    Assert-Load "must-block: $consumer loads every library file whose functions its loaded files call" ($missing.Count -eq 0) ($missing -join '; ')
}
foreach ($module in @($parsed.Keys | Where-Object { $_ -clike 'Gate*.ps1' -and $_ -cne 'GateCommon.ps1' })) {
    $loads = @(Get-DotSourced $parsed[$module] | Where-Object { $_ -clike 'Gate*.ps1' })
    Assert-Load "must-block: $module loads no other library module, so a consumer's load list stays its whole dependency" ($loads.Count -eq 0) ($loads -join ', ')
}

$trxHarness = [IO.File]::ReadAllText((Join-Path $PSScriptRoot 'Test-GateTrx.ps1'))
$withoutDouble = $trxHarness -replace "(?m)^\. \(Join-Path \`$PSScriptRoot 'gate-check-double\.ps1'\)\r?\n", ''
$planted = [Management.Automation.Language.Parser]::ParseInput($withoutDouble, [ref]$null, [ref]$null)
$plantedMissing = @(Get-MissingCalls $planted 'Test-GateTrx.ps1')
Assert-Load 'control: the TRX harness with its double load removed is caught missing Write-Row and Stop-Gate' (($plantedMissing -join ' ') -like '*Write-Row*' -and ($plantedMissing -join ' ') -like '*Stop-Gate*') ($plantedMissing -join '; ')

if ($failures) { Write-Output "$failures case(s) failed."; exit 1 }
Write-Output 'All cases passed.'
exit 0

$script:gateVerdicts = @{}
$script:gateSiblingVerdicts = @{}
$script:gateFingerprint = $null
$script:gateManifest = $null
$script:gateToolHashes = $null
$script:gateUsedModules = [Collections.Generic.SortedSet[string]]::new([StringComparer]::Ordinal)
$script:gateLeftoverPattern = '(^|/)(StrykerOutput|\.sonarqube)/'
$script:gateToolsRoot = Split-Path -Parent $PSScriptRoot

function Test-VerdictCarries([string]$name, $verdict) {
    $modules = Get-RecordedModules $verdict
    $verdict -and $null -ne $modules -and $verdict.verdict -eq 'PASS' -and $verdict.fingerprint -eq (Get-StepFingerprint $name -modules $modules)
}

function Get-CarriedVerdict([string]$name) {
    <#
    .SYNOPSIS
        The PASS verdict that carries step $name on the inputs as they are now: this checkout's own, else one another
        checkout of the same repo recorded on this machine for this account, since a fingerprint is the content of a
        step's inputs and nothing else; $null when none matches. A checkout's own entry for the step (or main's,
        inherited) that is not a PASS decides, so its own failure is never outvoted by another checkout's pass; an
        own PASS on other inputs, or one saved before verdicts recorded their modules, gives way to a sibling's.
    #>
    $own = $script:gateVerdicts[$name]
    if (Test-VerdictCarries $name $own) { return $own }
    if ($own -and $own.verdict -ne 'PASS') { return $null }
    foreach ($sibling in @($script:gateSiblingVerdicts[$name])) {
        if (Test-VerdictCarries $name $sibling) { return $sibling }
    }
    return $null
}

function Get-SiblingVerdicts([string]$repo, [string]$directory) {
    <#
    .SYNOPSIS
        {step: [verdict]} from every other checkout's verdict file for $repo on this machine, beside $directory under
        the account's gate records, each verdict stamped with the folder it came from.
    #>
    $siblings = @{}
    $root = Split-Path -Parent $directory
    if (-not $root -or -not (Test-Path -LiteralPath $root -PathType Container)) { return $siblings }
    $own = [IO.Path]::GetFullPath($directory).TrimEnd('\')
    foreach ($folder in [IO.Directory]::GetDirectories($root)) {
        if ([string]::Equals([IO.Path]::GetFullPath($folder).TrimEnd('\'), $own, [StringComparison]::OrdinalIgnoreCase)) { continue }
        $file = Join-Path $folder "$repo.$env:COMPUTERNAME.json"
        if (-not (Test-Path -LiteralPath $file -PathType Leaf)) { continue }
        try { $loaded = Get-Content -LiteralPath $file -Raw -ErrorAction Stop | ConvertFrom-Json -AsHashtable } catch { continue }
        if ($loaded -isnot [Collections.IDictionary]) { continue }
        foreach ($step in @($loaded.Keys)) {
            $verdict = $loaded[$step]
            if ($verdict -isnot [Collections.IDictionary] -or $verdict['verdict'] -ne 'PASS') { continue }
            $verdict['from'] = Split-Path -Leaf $folder
            if (-not $siblings.ContainsKey($step)) { $siblings[$step] = [Collections.Generic.List[object]]::new() }
            $siblings[$step].Add($verdict)
        }
    }
    return $siblings
}

function Get-StepCarryEvidence([string]$step, [string[]]$passable) {
    if (Test-VolatileStep $step) { return $null }
    $rows = @(@($script:gateVerdicts.Keys) + @($script:gateSiblingVerdicts.Keys) | Sort-Object -Unique)
    foreach ($row in $rows) {
        if ((Get-LongestStepPrefix $row $passable) -cne $step) { continue }
        $carried = Get-CarriedVerdict $row
        if ($carried) { return $carried.at }
    }
    return $null
}

function Resolve-OwedSteps {
    if ($null -ne $script:gateProbeSteps) { return }
    $passable = @($script:gateCatalogToolFiles.Keys) + @($script:gateDeclaredSteps)
    $owed = @($script:gateOriginStep, $script:gateDependencyOrderStep) + @($passable | Where-Object { -not (Get-StepCarryEvidence $_ $passable) })
    Write-Host "Owed steps: $($owed -join ', ')"
}

function Use-GateModule([string]$module) {
    <#
    .SYNOPSIS
        Records that the step in flight called a verdict-bearing module, so that module, and no other Tools file of
        the gate library, joins the step's fingerprint when its verdict is saved.
    #>
    [void]$script:gateUsedModules.Add($module)
}

function Get-RecordedModules($verdict) {
    <#
    .SYNOPSIS
        The modules a saved verdict's step called, or $null for a verdict saved before steps recorded them, which
        carries nothing.
    #>
    if ($verdict -is [Collections.IDictionary] -and $verdict.Contains('modules')) { return [string[]]@($verdict['modules']) }
    return $null
}

function Get-StepToolFiles([string]$name, [AllowNull()][string[]]$modules) {
    <#
    .SYNOPSIS
        The Tools files a step's verdict depends on: its catalog's scripts for a catalog step, and the verdict-bearing
        modules the step itself called. The library's bookkeeping decides only whether a step runs, never its verdict,
        so it joins no step's fingerprint, and no list of modules is kept: a module that decides a verdict says so.
    #>
    $files = @()
    foreach ($catalog in @($script:gateCatalogToolFiles.Keys)) {
        if ($name.StartsWith($catalog, [StringComparison]::Ordinal)) { $files += $script:gateCatalogToolFiles[$catalog] }
    }
    $files += @($modules)
    return @($files | Where-Object { $_ } | Sort-Object -Unique)
}

function Get-StepFingerprint([string]$name, $manifest = $script:gateManifest, $tools = $script:gateToolHashes, [AllowNull()][string[]]$modules = $null) {
    $patterns = Get-StepInputPatterns $name
    $builder = [Text.StringBuilder]::new()
    foreach ($file in $manifest.Keys) {
        if (@($patterns | Where-Object { $file -like $_ }).Count -gt 0) {
            [void]$builder.Append($file).Append(':').Append($manifest[$file]).Append("`n")
        }
    }
    foreach ($file in Get-StepToolFiles $name $modules) {
        [void]$builder.Append('Tools/').Append($file).Append(':').Append($tools[$file]).Append("`n")
    }
    $sha = [Security.Cryptography.SHA256]::Create()
    return [Convert]::ToHexString($sha.ComputeHash([Text.Encoding]::UTF8.GetBytes($builder.ToString())))
}

function Get-FileHashHex([string]$path) {
    $sha = [Security.Cryptography.SHA256]::Create()
    return [Convert]::ToHexString($sha.ComputeHash([IO.File]::ReadAllBytes($path)))
}

function Get-DeclaredMarkdownInputs {
    $declared = [Collections.Generic.HashSet[string]]::new([StringComparer]::Ordinal)
    foreach ($patterns in $script:gateStepInputs.Values) {
        foreach ($pattern in $patterns) {
            if ($pattern -match '(?i)\.md$' -and $pattern -notmatch '[*?\[\]]') { [void]$declared.Add($pattern) }
        }
    }
    return , $declared
}

function Get-GateManifest([string]$repoPath) {
    $declared = Get-DeclaredMarkdownInputs
    $files = [string[]]@(git -C $repoPath ls-files -co --exclude-standard |
        Where-Object { $declared.Contains($_) -or ($_ -notmatch '(?i)\.md$' -and $_ -notmatch '^\.github/' -and $_ -notmatch $script:gateLeftoverPattern) })
    [Array]::Sort($files, [StringComparer]::Ordinal)
    $manifest = [ordered]@{}
    foreach ($file in $files) {
        $full = Join-Path $repoPath $file
        if (Test-Path -LiteralPath $full -PathType Leaf) { $manifest[$file] = Get-FileHashHex $full }
    }
    return $manifest
}

function Get-GateToolHashes {
    <#
    .SYNOPSIS
        The hash of every gate library module (Gates/Gate*.ps1, by name) and every catalog script, taken at the gate's
        start so a library edit mid-run is drift, from which each step's fingerprint takes the files it depends on.
    #>
    $names = [Collections.Generic.SortedSet[string]]::new([StringComparer]::Ordinal)
    $gates = Join-Path $script:gateToolsRoot 'Gates'
    if (Test-Path -LiteralPath $gates -PathType Container) {
        foreach ($file in [IO.Directory]::GetFiles($gates, 'Gate*.ps1')) { [void]$names.Add('Gates/' + [IO.Path]::GetFileName($file)) }
    }
    foreach ($files in @($script:gateCatalogToolFiles.Values)) { foreach ($file in $files) { [void]$names.Add($file) } }
    $hashes = [ordered]@{}
    foreach ($file in $names) {
        $full = Join-Path $script:gateToolsRoot $file
        if (Test-Path -LiteralPath $full -PathType Leaf) { $hashes[$file] = Get-FileHashHex $full }
    }
    return $hashes
}

function Get-GateFingerprint([string]$repoPath, $manifest = $null, $tools = $null) {
    if ($null -eq $manifest) { $manifest = Get-GateManifest $repoPath }
    if ($null -eq $tools) { $tools = Get-GateToolHashes }
    $builder = [Text.StringBuilder]::new()
    foreach ($file in $manifest.Keys) { [void]$builder.Append($file).Append(':').Append($manifest[$file]).Append("`n") }
    foreach ($file in $tools.Keys) { [void]$builder.Append('Tools/').Append($file).Append(':').Append($tools[$file]).Append("`n") }
    $sha = [Security.Cryptography.SHA256]::Create()
    return [Convert]::ToHexString($sha.ComputeHash([Text.Encoding]::UTF8.GetBytes($builder.ToString())))
}

function Save-StepVerdict([string]$name, [string]$verdict) {
    if (-not $script:gateVerdictsFile) { return }
    $modules = [string[]]@($script:gateUsedModules)
    $script:gateVerdicts[$name] = @{ verdict = $verdict; fingerprint = (Get-StepFingerprint $name -modules $modules); modules = $modules; at = (Get-Date).ToString('o') }
    Write-GateStateFile $script:gateVerdictsFile ($script:gateVerdicts | ConvertTo-Json -Depth 4)
}

function Revoke-DriftedVerdicts {
    if (-not $script:gateVerdictsFile) { return $false }
    $manifestNow = Get-GateManifest $script:gateRepoPath
    $toolsNow = Get-GateToolHashes
    if ((Get-GateFingerprint $script:gateRepoPath $manifestNow $toolsNow) -eq $script:gateFingerprint) { return $false }
    $revoked = @()
    foreach ($name in @($script:gateVerdicts.Keys)) {
        $modules = Get-RecordedModules $script:gateVerdicts[$name]
        $atStart = Get-StepFingerprint $name -modules $modules
        if ($script:gateVerdicts[$name].fingerprint -eq $atStart -and (Get-StepFingerprint $name $manifestNow $toolsNow $modules) -ne $atStart) {
            $script:gateVerdicts.Remove($name)
            $revoked += $name
        }
    }
    Write-GateStateFile $script:gateVerdictsFile ($script:gateVerdicts | ConvertTo-Json -Depth 4)
    Write-Host "INPUTS CHANGED DURING THE GATE: $($revoked.Count) step verdict(s) whose inputs moved will not be carried"
    return $true
}

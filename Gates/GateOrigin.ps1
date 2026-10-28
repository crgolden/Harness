function Get-PackageSourceFiles([string]$directory) {
    if (Test-Path -LiteralPath (Join-Path $directory '.git')) {
        return @(git -C $directory ls-files -co --exclude-standard | Where-Object { $_ -match '(^|/)(package\.json|[^/]+\.csproj)$' })
    }
    $files = Get-ChildItem -LiteralPath $directory -Recurse -File -Include 'package.json', '*.csproj' -ErrorAction SilentlyContinue
    return @($files | ForEach-Object { [IO.Path]::GetRelativePath($directory, $_.FullName) -replace '\\', '/' } |
            Where-Object { $_ -notmatch '(^|/)(node_modules|bin|obj)/' })
}

function Get-WorkspacePackageVersions([string]$workspace) {
    $versions = @{}
    foreach ($directory in @(Get-ChildItem -LiteralPath $workspace -Directory -ErrorAction SilentlyContinue)) {
        foreach ($file in Get-PackageSourceFiles $directory.FullName) {
            $full = Join-Path $directory.FullName $file
            if (-not (Test-Path -LiteralPath $full -PathType Leaf)) { continue }
            $text = Get-Content -LiteralPath $full -Raw
            if ($file -match '(^|/)package\.json$') {
                $manifest = $text | ConvertFrom-Json -AsHashtable
                if ($manifest['name'] -and $manifest['version']) { $versions[$manifest['name']] = $manifest['version'] }
            }
            elseif ($text -match '<PackageId>\s*([^<\s]+)\s*</PackageId>') {
                $id = $Matches[1]
                if ($text -match '<Version>\s*([^<\s]+)\s*</Version>') { $versions[$id] = $Matches[1] }
            }
        }
    }
    return $versions
}

function Get-AncestorDirectories([string]$repoPath, [string]$file) {
    $directories = [Collections.Generic.List[string]]::new()
    $directories.Add($repoPath)
    $current = $repoPath
    foreach ($segment in @($file -split '/' | Select-Object -SkipLast 1)) {
        $current = Join-Path $current $segment
        $directories.Add($current)
    }
    return $directories
}

function Get-PrivateNpmScopes([string]$repoPath, [string]$file) {
    $scopes = [Collections.Generic.List[string]]::new()
    foreach ($directory in Get-AncestorDirectories $repoPath $file) {
        $npmrc = Join-Path $directory '.npmrc'
        if (-not (Test-Path -LiteralPath $npmrc -PathType Leaf)) { continue }
        foreach ($line in Get-Content -LiteralPath $npmrc) {
            if ($line -match '^\s*(@[^:\s]+):registry\s*=\s*(\S+)' -and $Matches[2] -notmatch 'registry\.npmjs\.org') { $scopes.Add($Matches[1]) }
        }
    }
    return , $scopes
}

function Get-PrivateNuGetPatterns([string]$repoPath, [string]$file) {
    $patterns = [Collections.Generic.List[string]]::new()
    foreach ($directory in Get-AncestorDirectories $repoPath $file) {
        foreach ($config in @(Get-ChildItem -LiteralPath $directory -File -Filter 'nuget.config' -ErrorAction SilentlyContinue)) {
            [xml]$doc = Get-Content -LiteralPath $config.FullName -Raw
            $sources = @{}
            foreach ($add in @($doc.configuration.packageSources.add)) { if ($add.key) { $sources[$add.key] = $add.value } }
            foreach ($source in @($doc.configuration.packageSourceMapping.packageSource)) {
                if (-not $source -or "$($sources[$source.key])" -match 'api\.nuget\.org' -or -not $sources.ContainsKey($source.key)) { continue }
                foreach ($package in @($source.package)) { if ($package.pattern) { $patterns.Add($package.pattern) } }
            }
        }
    }
    return , $patterns
}

function Test-NuGetPatternMatch([string]$name, [string[]]$patterns) {
    foreach ($pattern in $patterns) {
        if ([string]::Equals($name, $pattern, [StringComparison]::OrdinalIgnoreCase)) { return $true }
        if ($pattern.EndsWith('*') -and $name.StartsWith($pattern.TrimEnd('*'), [StringComparison]::OrdinalIgnoreCase)) { return $true }
    }
    return $false
}

function Get-SharedPackagePins([string]$repoPath) {
    $pins = [Collections.Generic.List[object]]::new()
    foreach ($file in @(git -C $repoPath ls-files -co --exclude-standard)) {
        $full = Join-Path $repoPath $file
        if (-not (Test-Path -LiteralPath $full -PathType Leaf)) { continue }
        if ($file -match '(^|/)package\.json$') {
            $scopes = Get-PrivateNpmScopes $repoPath $file
            if ($scopes.Count -eq 0) { continue }
            $manifest = Get-Content -LiteralPath $full -Raw | ConvertFrom-Json -AsHashtable
            foreach ($section in 'dependencies', 'devDependencies') {
                if (-not $manifest[$section]) { continue }
                foreach ($package in @($manifest[$section].Keys)) {
                    if ($scopes | Where-Object { $package.StartsWith("$_/") }) {
                        $pins.Add([ordered]@{ package = $package; pin = $manifest[$section][$package]; file = $file })
                    }
                }
            }
        }
        elseif ($file -match '\.(csproj|props)$') {
            $patterns = Get-PrivateNuGetPatterns $repoPath $file
            if ($patterns.Count -eq 0) { continue }
            foreach ($match in [regex]::Matches((Get-Content -LiteralPath $full -Raw), '<PackageReference\s+Include="([^"]+)"\s+Version="([^"]+)"')) {
                if (Test-NuGetPatternMatch $match.Groups[1].Value $patterns) {
                    $pins.Add([ordered]@{ package = $match.Groups[1].Value; pin = $match.Groups[2].Value; file = $file })
                }
            }
        }
    }
    return , $pins
}

function Invoke-DependencyOrderStep {
    Use-GateModule 'Gates/GateOrigin.ps1'
    $pins = Get-SharedPackagePins $script:gateRepoPath
    if ($pins.Count -eq 0) { Write-Row $script:gateDependencyOrderStep 'PASS' 'pins no shared package'; return }
    $localVersions = Get-WorkspacePackageVersions (Split-Path -Parent $script:gateRepoPath)
    $behind = [Collections.Generic.List[string]]::new()
    foreach ($pin in $pins) {
        $local = $localVersions[$pin.package]
        if ($null -eq $local) {
            $behind.Add("$($pin.file) pins $($pin.package) $($pin.pin), but the package's source is not in the workspace, so the order cannot be checked")
            continue
        }
        $pinned = $null
        $current = $null
        if (-not [version]::TryParse($pin.pin, [ref]$pinned) -or -not [version]::TryParse($local, [ref]$current)) {
            $behind.Add("$($pin.file) pins $($pin.package) $($pin.pin) against local $local, and one of them is not a plain version")
            continue
        }
        if ($current -gt $pinned) {
            $behind.Add("$($pin.file) pins $($pin.package) $($pin.pin) but the local package is at $local")
        }
    }
    if ($behind.Count -gt 0) {
        $behind | ForEach-Object { Write-Host "    $_" }
        Stop-Gate $script:gateDependencyOrderStep 'gate and publish the package, bump this repo''s pin to it, then gate this repo'
    }
    Write-Row $script:gateDependencyOrderStep 'PASS' "$($pins.Count) pin(s) current with the local package"
}

function Invoke-OriginFetch([string]$repoPath) {
    $savedPrompt = $env:GIT_TERMINAL_PROMPT
    $savedInteractive = $env:GCM_INTERACTIVE
    $env:GIT_TERMINAL_PROMPT = '0'
    $env:GCM_INTERACTIVE = 'never'
    try {
        $global:LASTEXITCODE = $null
        $fetched = @(git -C $repoPath fetch --quiet origin 2>&1 | ForEach-Object { "$_" })
        [pscustomobject]@{ ExitCode = $global:LASTEXITCODE; Output = ($fetched -join ' ').Trim() }
    }
    finally {
        $env:GIT_TERMINAL_PROMPT = $savedPrompt
        $env:GCM_INTERACTIVE = $savedInteractive
    }
}

function Invoke-OriginFastForward([string]$repoPath) {
    <#
    .SYNOPSIS
        Catches an operator gate's checkout up to origin's default branch before the gate reads its inputs, so the
        fingerprint and the owed steps describe the merged tree. It acts only on the default branch itself or on
        the session's own branch (named by CLAUDE_CODE_SESSION_ID), and only when the checkout is behind and
        holds nothing origin lacks: it fast-forwards over its unstaged edits, which git refuses to overwrite.
        Staged changes, commits origin lacks, a triage gate or any git failure are left for
        Invoke-OriginCurrencyStep to report; a refused catch-up is recorded so that step names it.
    #>
    $script:gateOriginFastForward = $null
    $script:gateOriginCatchUpFailed = $false
    if (-not [string]::IsNullOrWhiteSpace($env:GATE_RUN_KIND)) { return }
    if ((Invoke-OriginFetch $repoPath).ExitCode -ne 0) { return }
    $default = git -C $repoPath symbolic-ref --quiet --short refs/remotes/origin/HEAD 2>$null
    if ($global:LASTEXITCODE -ne 0 -or [string]::IsNullOrWhiteSpace($default)) { return }
    $branch = git -C $repoPath symbolic-ref --quiet --short HEAD 2>$null
    if ($global:LASTEXITCODE -ne 0) { return }
    $onDefault = [string]::Equals("origin/$branch", $default, [StringComparison]::Ordinal)
    $onSession = -not [string]::IsNullOrWhiteSpace($env:CLAUDE_CODE_SESSION_ID) -and
        [string]::Equals($branch, $env:CLAUDE_CODE_SESSION_ID, [StringComparison]::Ordinal)
    if (-not $onDefault -and -not $onSession) { return }
    $behind = git -C $repoPath rev-list --count "HEAD..$default" 2>$null
    if ($global:LASTEXITCODE -ne 0 -or [int]$behind -eq 0) { return }
    $ahead = git -C $repoPath rev-list --count "$default..HEAD" 2>$null
    if ($global:LASTEXITCODE -ne 0) { return }
    $null = git -C $repoPath diff --cached --quiet 2>$null
    if ($global:LASTEXITCODE -ne 0) { return }
    if ([int]$ahead -ne 0) { return }
    $global:LASTEXITCODE = $null
    $merged = @(git -C $repoPath merge --ff-only --quiet $default 2>&1 | ForEach-Object { "$_" })
    if ($global:LASTEXITCODE -eq 0) { $script:gateOriginFastForward = "fast-forwarded $behind commit(s) to $default"; return }
    $script:gateOriginCatchUpFailed = $true
    $script:gateOriginFastForward = "git merge --ff-only $default failed (exit $global:LASTEXITCODE): $(($merged -join ' ').Trim())"
}

function Invoke-OriginCurrencyStep {
    Use-GateModule 'Gates/GateOrigin.ps1'
    $step = $script:gateOriginStep
    $repoPath = $script:gateRepoPath
    if ([string]::IsNullOrWhiteSpace($env:GATE_RUN_KIND)) {
        $fetch = Invoke-OriginFetch $repoPath
        if ($fetch.ExitCode -ne 0) {
            Stop-Gate $step "git fetch origin failed (exit $($fetch.ExitCode)), so this checkout cannot be shown current: $($fetch.Output)"
        }
        $source = 'fetched now'
    }
    elseif ([string]::Equals($env:GATE_RUN_KIND, 'triage', [StringComparison]::Ordinal)) {
        $source = 'against the refs the triage runner fetched for this job'
    }
    else {
        Stop-Gate $step "GATE_RUN_KIND '$env:GATE_RUN_KIND' is neither unset nor triage"
    }
    $default = git -C $repoPath symbolic-ref --quiet --short refs/remotes/origin/HEAD 2>$null
    if ($global:LASTEXITCODE -ne 0 -or [string]::IsNullOrWhiteSpace($default)) {
        Stop-Gate $step "origin/HEAD is not set, so the default branch is unknown: run git -C `"$repoPath`" remote set-head origin --auto"
    }
    $behind = git -C $repoPath rev-list --count "HEAD..$default" 2>$null
    if ($global:LASTEXITCODE -ne 0 -or [string]::IsNullOrWhiteSpace($behind)) { Stop-Gate $step "could not compare HEAD with $default" }
    if ([int]$behind -gt 0) {
        if ($script:gateOriginCatchUpFailed) { Stop-Gate $step $script:gateOriginFastForward }
        $ahead = git -C $repoPath rev-list --count "$default..HEAD" 2>$null
        $remedy = if ([int]$ahead -eq 0) { "git -C `"$repoPath`" merge --ff-only $default" }
            else { "HEAD also holds $ahead commit(s) $default lacks, so merge or rebase onto $default" }
        Stop-Gate $step "$behind commit(s) behind ${default}: set uncommitted edits aside, $remedy, reapply them, then gate"
    }
    Write-Row $step 'PASS' "HEAD contains $default ($source$(if ($script:gateOriginFastForward) { "; $script:gateOriginFastForward" }))"
}

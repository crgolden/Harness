function Get-NodeInstallDrift([string]$packageRoot) {
    <#
    .SYNOPSIS
        Every way node_modules under $packageRoot differs from the tree package-lock.json names, read from each
        installed package's own package.json: a package at another version, a package the lockfile requires that is
        not installed, and a top-level package the lockfile no longer names. npm's summary of the install
        (node_modules\.package-lock.json) is never trusted: npm install --package-lock-only rewrites it without
        installing anything. Optional and dev-optional packages may be absent (npm skips other platforms' builds),
        and a linked workspace package is not compared. Empty means npm ci would install exactly this tree.
    #>
    $wanted = (Get-Content -LiteralPath (Join-Path $packageRoot 'package-lock.json') -Raw | ConvertFrom-Json -AsHashtable)['packages']
    $drift = [Collections.Generic.List[string]]::new()
    foreach ($path in $wanted.Keys | Where-Object { $_ -like 'node_modules/*' } | Sort-Object) {
        $entry = $wanted[$path]
        if ($entry['link']) { continue }
        $manifest = Join-Path $packageRoot ($path.Replace('/', '\') + '\package.json')
        if (-not (Test-Path -LiteralPath $manifest)) {
            if (-not ($entry['optional'] -or $entry['devOptional'])) { $drift.Add("$path $($entry['version']) not installed") }
            continue
        }
        $installed = (Get-Content -LiteralPath $manifest -Raw | ConvertFrom-Json -AsHashtable)['version']
        if ($installed -cne $entry['version']) { $drift.Add("$path installed $installed, lockfile $($entry['version'])") }
    }
    $top = Join-Path $packageRoot 'node_modules'
    $folders = foreach ($folder in Get-ChildItem -LiteralPath $top -Directory -Force | Where-Object Name -notlike '.*') {
        if ($folder.Name.StartsWith('@')) { Get-ChildItem -LiteralPath $folder.FullName -Directory -Force | ForEach-Object { "node_modules/$($folder.Name)/$($_.Name)" } }
        else { "node_modules/$($folder.Name)" }
    }
    foreach ($path in $folders | Where-Object { -not $wanted.ContainsKey($_) } | Sort-Object) {
        $drift.Add("$path installed but not in the lockfile")
    }
    @($drift)
}

function Assert-NodeInstallCurrent([string]$packageRoot) {
    Use-GateModule 'Gates/GateNode.ps1'
    $drift = @(Get-NodeInstallDrift $packageRoot)
    if ($drift.Count) {
        Stop-Gate 'node_modules install markers' ("node_modules is not the lockfile's tree ({0} difference(s): {1}); run npm ci deliberately first" -f $drift.Count, (($drift | Select-Object -First 3) -join '; '))
    }
}

function Get-PublishCommandPrefix([string]$workflowText) {
    $line = $workflowText -split "`n" | Where-Object { $_ -match 'publish-bdd-results\s+--app' } | Select-Object -First 1
    if (-not $line) { return @() }
    $before = ($line -replace '^\s*(run:\s*)?', '') -replace 'publish-bdd-results\s+--app.*$', ''
    return @($before.Trim() -split '\s+' | Where-Object { $_ })
}

function Invoke-PublishCommand([string[]]$prefix, [string]$app, [string]$workingDirectory, [string]$messagesPath, [switch]$withoutApp) {
    $savedRunId = $env:GITHUB_RUN_ID
    Remove-Item Env:GITHUB_RUN_ID -ErrorAction SilentlyContinue
    Push-Location $workingDirectory
    try {
        $command = $prefix[0]
        $arguments = @($prefix | Select-Object -Skip 1)
        $appArguments = $withoutApp ? @() : @('--app', $app)
        return (& $command @arguments publish-bdd-results @appArguments $messagesPath 2>&1 | ForEach-Object { "$_" }) -join "`n"
    }
    finally {
        Pop-Location
        if ($savedRunId) { $env:GITHUB_RUN_ID = $savedRunId }
    }
}

function Test-PublishCommandLine([string]$name, [string]$workflowPath, [string]$app, [string]$workingDirectory, [string]$messagesPath) {
    Use-GateModule 'Gates/GateNode.ps1'
    $prefix = Get-PublishCommandPrefix (Get-Content -LiteralPath $workflowPath -Raw)
    if ($prefix.Count -eq 0) { Stop-Gate $name "no 'publish-bdd-results --app' line in $workflowPath" }
    if (-not (Test-Path -LiteralPath (Join-Path $workingDirectory $messagesPath))) {
        Stop-Gate $name "no Cucumber messages at $messagesPath under $workingDirectory, so the E2E step wrote none"
    }
    $reached = Invoke-PublishCommand $prefix $app $workingDirectory $messagesPath
    if ($reached -notmatch 'GITHUB_RUN_ID is not set') {
        Stop-Gate $name "the workflow's '$($prefix -join ' ') publish-bdd-results' never reached the CLI: $reached"
    }
    $control = Invoke-PublishCommand $prefix $app $workingDirectory $messagesPath -withoutApp
    if ($control -notmatch 'Usage: publish-bdd-results') {
        Stop-Gate $name "control: without --app the command did not stop at usage, so this step cannot tell a broken line from a working one: $control"
    }
    Write-Row $name 'PASS' "'$($prefix -join ' ')' reached the CLI; the control without --app stopped at usage"
}

function Test-PlaywrightBrowsersInstalled([string[]]$dryRunLines) {
    $locations = @($dryRunLines | ForEach-Object { if ($_ -match '^\s*Install location:\s*(\S.*?)\s*$') { $Matches[1] } })
    if ($locations.Count -eq 0) { return $false }
    return @($locations | Where-Object { -not (Test-Path -LiteralPath (Join-Path $_ 'INSTALLATION_COMPLETE') -PathType Leaf) }).Count -eq 0
}

function Install-PlaywrightBrowsers([string]$name, [scriptblock]$dryRun, [scriptblock]$install) {
    Use-GateModule 'Gates/GateNode.ps1'
    if (Test-PlaywrightBrowsersInstalled @(& $dryRun 2>&1 | ForEach-Object { "$_" })) {
        Write-Row $name 'PASS' 'every browser this Playwright version needs is already installed (INSTALLATION_COMPLETE present)'
        return
    }
    $global:LASTEXITCODE = $null
    & $install
    $null = Test-Exit $name
}

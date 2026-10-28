# GATE-STEP: Gate Node checker harness
<#
.SYNOPSIS
    Harness for GateNode.ps1, against the bookkeeping double, with GateExit.ps1 because Install-PlaywrightBrowsers
    judges its install through Test-Exit: node_modules drift is read from each installed package against the lockfile;
    the Playwright step installs only when a browser is incomplete; and the workflow's publish line passes only when it
    reaches the CLI while its control, the same line without --app, stops at usage.
#>
. (Join-Path $PSScriptRoot 'gate-check-double.ps1')
. (Join-Path $PSScriptRoot 'GateExit.ps1')
. (Join-Path $PSScriptRoot 'GateNode.ps1')

$work = New-ClaudeTempPath "gate-node-test-$PID"
New-Item -ItemType Directory -Force -Path $work | Out-Null
try {
    function New-NodeFixture([string]$Name, [hashtable]$Locked, [hashtable]$Installed) {
        $root = Join-Path $work "node-$Name"
        $packages = [ordered]@{ '' = @{ name = 'fixture' } }
        foreach ($package in $Locked.Keys) { $packages["node_modules/$package"] = $Locked[$package] }
        New-Item -ItemType Directory -Force -Path (Join-Path $root 'node_modules') | Out-Null
        @{ lockfileVersion = 3; packages = $packages } | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath (Join-Path $root 'package-lock.json') -Encoding utf8
        @{ lockfileVersion = 3; packages = $packages } | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath (Join-Path $root 'node_modules\.package-lock.json') -Encoding utf8
        foreach ($package in $Installed.Keys) {
            $folder = Join-Path $root ('node_modules\' + $package.Replace('/', '\'))
            New-Item -ItemType Directory -Force -Path $folder | Out-Null
            @{ name = $package; version = $Installed[$package] } | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $folder 'package.json') -Encoding utf8
        }
        $root
    }

    $drift = @(Get-NodeInstallDrift (New-NodeFixture 'current' @{ '@scope/a' = @{ version = '1.0.0' }; 'b' = @{ version = '2.0.0' } } @{ '@scope/a' = '1.0.0'; 'b' = '2.0.0' }))
    Assert-Check 'control: an install matching the lockfile has no drift' ($drift.Count -eq 0) ($drift -join ' | ')
    $drift = @(Get-NodeInstallDrift (New-NodeFixture 'stale' @{ 'b' = @{ version = '2.1.0' } } @{ 'b' = '2.0.0' }))
    Assert-Check 'must-block: a package at another version than the lockfile is drift, whatever npm''s summary says' ($drift.Count -eq 1 -and $drift[0] -ceq 'node_modules/b installed 2.0.0, lockfile 2.1.0') ($drift -join ' | ')
    $drift = @(Get-NodeInstallDrift (New-NodeFixture 'optional' @{ 'b' = @{ version = '2.0.0' }; 'c' = @{ version = '3.0.0'; optional = $true }; 'd' = @{ version = '4.0.0'; devOptional = $true } } @{ 'b' = '2.0.0' }))
    Assert-Check 'control: an optional package left uninstalled is no drift' ($drift.Count -eq 0) ($drift -join ' | ')
    $drift = @(Get-NodeInstallDrift (New-NodeFixture 'required' @{ 'b' = @{ version = '2.0.0' }; 'c' = @{ version = '3.0.0' } } @{ 'b' = '2.0.0' }))
    Assert-Check 'must-block: a required package not installed is drift' ($drift.Count -eq 1 -and $drift[0] -ceq 'node_modules/c 3.0.0 not installed') ($drift -join ' | ')
    $drift = @(Get-NodeInstallDrift (New-NodeFixture 'extraneous' @{ 'b' = @{ version = '2.0.0' } } @{ 'b' = '2.0.0'; '@old/linter' = '1.0.0' }))
    Assert-Check 'must-block: a top-level package the lockfile no longer names is drift' ($drift.Count -eq 1 -and $drift[0] -ceq 'node_modules/@old/linter installed but not in the lockfile') ($drift -join ' | ')
    $result = Invoke-Check { Assert-NodeInstallCurrent (New-NodeFixture 'assert' @{ 'b' = @{ version = '2.1.0' } } @{ 'b' = '2.0.0' }) }
    Assert-Check 'must-block: an install that is not the lockfile''s tree stops the gate' ($result.Stopped -and $result.Rows[0].Detail -like "node_modules is not the lockfile's tree (1 difference(s)*") (Format-Check $result)
    Assert-Check 'must-pass: the install check records its own module' ($result.Modules -ccontains 'Gates/GateNode.ps1') ($result.Modules -join ', ')

    foreach ($case in 'complete', 'incomplete', 'unknown', 'install-fails') {
        $browsers = Join-Path $work "ms-playwright-$case"
        $complete = Join-Path $browsers 'chromium-1'
        $partial = Join-Path $browsers 'chromium_headless_shell-1'
        New-Item -ItemType Directory -Force -Path $complete, $partial | Out-Null
        New-Item -ItemType File -Force -Path (Join-Path $complete 'INSTALLATION_COMPLETE') | Out-Null
        if ($case -ceq 'complete') { New-Item -ItemType File -Force -Path (Join-Path $partial 'INSTALLATION_COMPLETE') | Out-Null }
        $listing = if ($case -ceq 'unknown') { @('Version 1.0.0') } else { @("  Install location:    $complete", "  Install location:    $partial") }
        $installExit = if ($case -ceq 'install-fails') { 1 } else { 0 }
        $result = Invoke-Check { Install-PlaywrightBrowsers 'First step' { $listing } { Write-Host 'INSTALL ran'; $global:LASTEXITCODE = $installExit } }
        $ran = $result.Printed -like '*INSTALL ran*'
        switch ($case) {
            'complete' { Assert-Check 'control: every browser complete installs nothing and passes' (-not $ran -and -not $result.Stopped -and $result.Rows[0].Detail -like '*already installed*') (Format-Check $result) }
            'incomplete' { Assert-Check 'must-pass: a browser missing its completion marker is installed' ($ran -and -not $result.Stopped) (Format-Check $result) }
            'unknown' { Assert-Check 'must-pass: a dry run naming no install location installs' ($ran -and -not $result.Stopped) (Format-Check $result) }
            'install-fails' { Assert-Check 'must-block: a failed install stops the gate with its exit code' ($ran -and $result.Stopped -and $result.Rows[-1].Detail -ceq 'exit 1') (Format-Check $result) }
        }
    }

    $honest = Join-Path $work 'npx-honest.cmd'
    Set-Content -LiteralPath $honest -Encoding ascii -Value ('@echo off', 'set passthrough=', ':scan', 'if "%~1"=="" goto usage', 'if "%~1"=="publish-bdd-results" goto found',
        'if "%~1"=="--" set passthrough=1', 'if "%~1"=="--package" set passthrough=1', 'shift', 'goto scan', ':found', 'if not defined passthrough goto usage',
        'if not "%~2"=="--app" goto usage', 'echo GITHUB_RUN_ID is not set, so the run cannot be identified.', 'exit /b 1',
        ':usage', 'echo Usage: publish-bdd-results --app ^<app^> ^<cucumber-messages.ndjson^>', 'exit /b 1')
    $argv = Join-Path $work 'npx-argv.cmd'
    Set-Content -LiteralPath $argv -Encoding ascii -Value ('@echo off', 'echo ARGV1=[%1] ARGV2=[%2] ARGV3=[%3]', 'exit /b 1')
    $blind = Join-Path $work 'npx-blind.cmd'
    Set-Content -LiteralPath $blind -Encoding ascii -Value ('@echo off', 'echo GITHUB_RUN_ID is not set, so the run cannot be identified.', 'exit /b 1')
    Assert-Check 'one option before publish-bdd-results reaches the command in order' ((Invoke-PublishCommand @($argv, '--no') 'Probe' $work 'messages.ndjson') -like '*ARGV1=`[--no`] ARGV2=`[publish-bdd-results`] ARGV3=`[--app`]*')
    Assert-Check 'two options before publish-bdd-results reach the command in order' ((Invoke-PublishCommand @($argv, '--no', '--') 'Probe' $work 'messages.ndjson') -like '*ARGV1=`[--no`] ARGV2=`[--`] ARGV3=`[publish-bdd-results`]*')
    foreach ($case in 'broken', 'blind-control', 'no-line', 'no-messages', 'reaches', 'reaches-package') {
        $folder = Join-Path $work "publish-$case"
        New-Item -ItemType Directory -Force -Path $folder | Out-Null
        $fake = if ($case -ceq 'blind-control') { $blind } else { $honest }
        $options = switch ($case) { 'broken' { '--no' } 'reaches-package' { '--yes --package @crgolden/modules@0.18.0 --' } default { '--no --' } }
        $line = if ($case -ceq 'no-line') { '        run: npm test' } else { "        run: $fake $options publish-bdd-results --app Probe messages.ndjson" }
        $workflow = Join-Path $folder 'workflow.yml'
        Set-Content -LiteralPath $workflow -Value @('steps:', $line)
        if ($case -cne 'no-messages') { Set-Content -LiteralPath (Join-Path $folder 'messages.ndjson') -Value '{}' }
        $result = Invoke-Check { Test-PublishCommandLine 'First step' $workflow 'Probe' $folder 'messages.ndjson' }
        $detail = if ($result.Rows.Count) { $result.Rows[-1].Detail } else { '' }
        switch ($case) {
            'broken' { Assert-Check 'must-block: a publish line whose arguments never reach the CLI stops the gate' ($result.Stopped -and $detail -like '*never reached the CLI*Usage: publish-bdd-results*') (Format-Check $result) }
            'blind-control' { Assert-Check 'must-block: a command that cannot fail its control stops the gate' ($result.Stopped -and $detail -like 'control: without --app the command did not stop at usage*') (Format-Check $result) }
            'no-line' { Assert-Check 'must-block: a workflow with no publish line stops the gate' ($result.Stopped -and $detail -like "no 'publish-bdd-results --app' line*") (Format-Check $result) }
            'no-messages' { Assert-Check 'must-block: no Cucumber messages stops the gate' ($result.Stopped -and $detail -like 'no Cucumber messages at messages.ndjson*') (Format-Check $result) }
            'reaches' { Assert-Check 'control: a line that reaches the CLI, with a control stopping at usage, passes' (-not $result.Stopped -and $detail -like '*reached the CLI; the control without --app stopped at usage') (Format-Check $result) }
            'reaches-package' { Assert-Check 'control: a line naming a package passes the same way' (-not $result.Stopped -and $detail -like '*--package @crgolden/modules@0.18.0 --'' reached the CLI; the control without --app stopped at usage') (Format-Check $result) }
        }
    }
} finally {
    Remove-FixtureTree $work
}
Complete-CheckHarness

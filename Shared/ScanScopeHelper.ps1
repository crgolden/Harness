# ScanScopeHelper.ps1
# Dot-source this file to get Get-ScanExtensionMap and Get-ScanLanguage.
# This is the ONE definition of "which file extensions the fleet's catalog scripts look at, and what
# language each one is". Get-CommentCatalog.ps1 and Get-CodeStyleCatalog.ps1 both read it instead of
# carrying their own list, because they carried two different lists for months: the comment catalog
# covered these 15 extensions across 10 languages while the code-style catalog covered 3 (.cs/.ts/.tsx),
# so "N rows fleet-wide" from the two scripts silently meant two different fleets.
#
# Scanning an extension is NOT the same as enforcing a rule on it. A scanner that has no implementation
# for a language must say so rather than contribute zero rows, because a zero is otherwise read as
# compliance - see Tools/AGENTS/CODE-STYLE.md's "A zero for Curator is a coverage gap, not compliance".
# Get-CodeStyleCatalog.ps1 reports its unimplemented languages for exactly that reason.
#
# Repo scope lives in each catalog's own $exemptRepos: both exempt the private ops-only repos (Tools,
# psnpy), and Test-CodeStyleCatalog.ps1 fails when the two lists differ.

function Get-ScanExtensionMap {
    <#
        .SYNOPSIS
        The fleet's extension-to-language map, shared by every catalog script.
    #>
    return @{
        '.cs'    = 'CSharp'
        '.ts'    = 'TypeScript'
        '.tsx'   = 'TypeScript'
        '.mts'   = 'TypeScript'
        '.cts'   = 'TypeScript'
        '.js'    = 'JavaScript'
        '.mjs'   = 'JavaScript'
        '.cjs'   = 'JavaScript'
        '.astro' = 'Astro'
        '.css'   = 'Css'
        '.py'    = 'Python'
        '.ps1'   = 'PowerShell'
        '.psm1'  = 'PowerShell'
        '.sh'    = 'Shell'
        '.sql'   = 'Sql'
        '.yml'   = 'Yaml'
        '.yaml'  = 'Yaml'
    }
}

function Get-ScanLanguage {
    <#
        .SYNOPSIS
        The language for one file, or $null when the extension is out of scope entirely.
    #>
    param([Parameter(Mandatory)][System.IO.FileInfo]$File)

    $map = Get-ScanExtensionMap
    if ($map.ContainsKey($File.Extension)) { return $map[$File.Extension] }
    return $null
}

function Get-ScanArea {
    <#
        .SYNOPSIS
        The test area one repo-relative path belongs to: test-e2e, test-integration, test-unit,
        test-other, test-config or prod.

        .DESCRIPTION
        The four layers in AGENTS/TESTING.md are decided by the interface a test drives, and the project
        name is what records that decision, so the area derives from the path and nothing needs a special
        case. Renaming a misplaced tier is therefore what makes the detectors correct.

        Shared because both catalogs need it and neither may copy it. CODE-STYLE.md rule 18 binds the
        phase markers and COMMENTS.md's marker allowance follows the same boundary, so a path that read as
        E2E to one scanner and unit to the other would make a marker allowed and forbidden at once.

        REPO scope is each catalog's own $exemptRepos (Tools and psnpy in both), not this helper's.
    #>
    param([Parameter(Mandatory)][string]$RelativePath)

    if ($RelativePath -match '\.Tests\.E2E\\' -or $RelativePath -match '\\e2e\\') { return 'test-e2e' }
    if ($RelativePath -match '(^|\\)tests\\(.+\\)?[^\\]+\.spec\.(?:tsx?|mts|cts)$') { return 'test-e2e' }
    if ($RelativePath -match '\.Tests\.(Integration|Load)\\' -or $RelativePath -match '^[^\\]+\\integration\\') { return 'test-integration' }
    if ($RelativePath -match '\.Tests\.Unit\\') { return 'test-unit' }
    if ($RelativePath -match '\.Tests?\\' -or $RelativePath -match 'Tests?\.cs$') { return 'test-other' }
    # A Vitest spec sits beside its source, so only its suffix says it is a test. Checked before the walker
    # directory because the walker engine's own unit tests live inside it.
    if ($RelativePath -match '\.(spec|test)\.(?:tsx?|mts|cts)$') { return 'test-unit' }
    # The synthetic-walker engine in Modules drives every walker's browser, so it is E2E code in a src tree.
    # enroll-passkey.ts beside it is not: it is a headed operator script that waits for a human to sign in
    # and refuses to run under CI, so a test-only rule has nothing to say about it.
    if ($RelativePath -match '\\synthetic-walker\\(?!enroll-passkey\.ts$)') { return 'test-e2e' }
    if ($RelativePath -match '(^|\\)(playwright|vitest)\.config\.(?:tsx?|mts|cts)$') { return 'test-config' }
    return 'prod'
}

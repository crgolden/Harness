<#
.SYNOPSIS
    Catalogs non-doc, non-suppression comments across every repo in the crgolden workspace.

.DESCRIPTION
    Walks each independent repo under the workspace root, finds C#/.NET, TypeScript/Astro, Python,
    PowerShell, and Shell source, and emits one CSV row per comment line that is NOT: a docstring/
    comment-based-help block, a linter/analyzer suppression justification, or a bare AAA/Given-When-Then
    test-structuring marker. Those categories are allowed under Tools/AGENTS/COMMENTS.md and are emitted with
    Disposition=keep so a second run can confirm nothing else remains. A marker word followed by
    substantive text (e.g. "// Arrange — <rationale>") is NOT a bare marker — it's flagged separately as
    aaa-marker-with-narrative/trim-to-marker, since COMMENTS.md only allows the bare form. Hand-written
    C# `///` XML doc is flagged as xml-doc/delete-or-migrate rather than silently skipped — COMMENTS.md
    disallows it fleet-wide with no exception.

    A `/** */` JSDoc block emits ONE jsdoc-block/needs-triage row at its opening fence and its body is not
    swept, the same shape Get-CodeStyleCatalog.ps1 uses for a raw-string block: the block is the unit a
    human judges. The disposition is needs-triage rather than delete because COMMENTS.md disallows only
    JSDoc "that narrates implementation rather than state an API contract", and no regex decides that.

    Repos under `Tools/` and the private `psnpy` repo are exempt from Tools/AGENTS/COMMENTS.md (private,
    ops-only code with no public consumers) and produce no row. Get-CodeStyleCatalog.ps1 exempts the
    same repos, and its harness fails when the two `$exemptRepos` lists differ. The file-extension map
    is shared, via ..\Shared\ScanScopeHelper.ps1.

    This is a heuristic regex pass, not a language parser — it is meant to drive a manual disposition
    pass (test / doc / code-change / delete), not to auto-classify with certainty. Expect to eyeball the
    output, especially the NeedsTriage rows.

.PARAMETER WorkspaceRoot
    Root directory containing the independent per-repo git checkouts. Defaults to two levels above this
    script's own location (Tools/CommentAudit/), which resolves to the workspace root regardless of which
    machine or user profile it's run from. Throws if the resolved (or explicitly passed) path doesn't
    exist, rather than silently scanning zero files.

.PARAMETER OutputPath
    Where to write the CSV. Defaults to comment-catalog.csv under %TEMP%\claude (CLAUDE_CODE_TMPDIR when set),
    outside any synced or repo directory.

.PARAMETER Repo
    Scan one repo under the workspace root instead of all of them; repo names and the exempt list still
    resolve against the workspace root. With -RepoPath it is the name that repo's files carry.

.PARAMETER RepoPath
    Scan this one checkout, wherever it is, as the repo -Repo names (its folder name when -Repo is absent).
    A repo's gate.ps1 passes both through GateCommon's Invoke-CatalogSteps, so a worktree or a clone under
    another folder name is scanned and exempted as the repo it is.

.EXAMPLE
    .\Get-CommentCatalog.ps1 -OutputPath 'C:\scratch\comment-catalog.csv'
#>
[CmdletBinding()]
param(
    [string]$WorkspaceRoot = (Split-Path -Parent (Split-Path -Parent $PSScriptRoot)),
    [string]$OutputPath,
    [string]$Repo,
    [string]$RepoPath
)

$ErrorActionPreference = 'Stop'

. "$PSScriptRoot\..\Shared\ScanScopeHelper.ps1"
. "$PSScriptRoot\..\Hooks\ClaudeTemp.ps1"

if (-not $OutputPath) { $OutputPath = New-ClaudeTempPath 'comment-catalog.csv' }

if ($RepoPath) {
    $RepoPath = (Get-Item -LiteralPath $RepoPath).FullName
    if (-not $Repo) { $Repo = Split-Path -Leaf $RepoPath }
    $WorkspaceRoot = Split-Path -Parent $RepoPath
}
if (-not (Test-Path -LiteralPath $WorkspaceRoot -PathType Container)) {
    throw "WorkspaceRoot '$WorkspaceRoot' does not exist. Pass -WorkspaceRoot explicitly if the script was moved."
}
$WorkspaceRoot = (Get-Item -LiteralPath $WorkspaceRoot).FullName

# 'dist-test' is Modules' tsc outDir for the test build (tsconfig.test.json 'outDir', run by the 'test'
# script, gitignored) — a compiled mirror of src/, so its comments are the same authored lines seen twice
# and fixing them there would be undone by the next build. Named exactly rather than matched as 'dist*',
# for the reason the Migrations note below records: a loose match silently hides authored directories.
$excludeDirNames = @(
    'bin', 'obj', 'node_modules', 'dist', 'dist-test', '.angular', 'venv', '.venv', '__pycache__',
    '.git', '.vs', '.idea', 'lib', 'coverage', '.astro', 'playwright-report', 'cucumber-report', '.features-gen', 'test-results', 'StrykerOutput'
)

# EF Core scaffolds these, so their comments are generator output rather than authored prose. Scoped to
# .cs because Curator's hand-written SQL migrations live in a directory named 'migrations' too, and
# -contains is case-insensitive — an unscoped entry hid all 44 of them even once '.sql' was mapped.
$csharpOnlyExcludeDirNames = @('Migrations')

# Repo-relative path fragments to exclude wholesale (generated mirrors, etc.)
$excludePathFragments = @(
    '\Curator\build\lib\',
    '\Tools\CommentAudit\'
)

# Repos exempt from Tools/AGENTS/COMMENTS.md's comment policy entirely: private, ops-only code with no public
# consumers. Add-Row drops their rows. Get-CodeStyleCatalog.ps1 carries the same list, and
# Test-CodeStyleCatalog.ps1 fails when the two differ.
$exemptRepos = @('Tools', 'Harness', 'psnpy')

function Test-Excluded([System.IO.FileInfo]$file) {
    $parts = $file.FullName.Split([System.IO.Path]::DirectorySeparatorChar)
    foreach ($p in $parts) {
        if ($excludeDirNames -contains $p) { return $true }
        if ($file.Extension -eq '.cs' -and $csharpOnlyExcludeDirNames -contains $p) { return $true }
        # Virtualenvs are named freely (.venv-validate, venv311, ...), so an exact-name list misses them
        # and their generated Activate.ps1 shows up as a repo's own narrative comments.
        if ($p -match '^\.?venv') { return $true }
    }
    # Minified CSS is generator output, and its licence header is one row on a single multi-kilobyte line.
    # 'lib', 'dist' and 'node_modules' above already catch most vendored CSS; this catches the rest.
    if ($file.Name -like '*.min.css') { return $true }
    foreach ($frag in $excludePathFragments) {
        if ($file.FullName -like "*$frag*") { return $true }
    }
    return $false
}

function Get-RepoName([string]$fullPath, [string]$root) {
    if ($RepoPath -and $fullPath.StartsWith($RepoPath + '\', [StringComparison]::OrdinalIgnoreCase)) { return $Repo }
    $rel = $fullPath.Substring($root.Length).TrimStart('\')
    return $rel.Split('\')[0]
}

$suppressionPattern = '(#pragma\s+warning\s+(disable|restore)|eslint-disable|SuppressMessage|#\s*noqa|#\s*type:\s*ignore|PSScriptAnalyzer\s+disable|//\s*Stryker\s+disable\s+once\s+[A-Za-z]+(\s*,\s*[A-Za-z]+)*\s*:\s*\S)'
# Only a single bare marker is allowed. A combined "// Act + Assert" means the test acts and asserts in
# one step, which is the smell the separation exists to expose — it stays flagged, not whitelisted.
$aaaBarePattern = '^\s*(//|#)\s*(Arrange|Act|Assert|Given|When|Then)\s*$'
$aaaNarrativePattern = '^\s*(//|#)\s*(Arrange|Act|Assert|Given|When|Then)\b\s*[:—-]'
$bannerPattern = '^\s*(//|#)\s*[\u2500\u2550\u2014\-=]{3,}'
# COMMENTS.md allow-list, "the version annotation on a SHA-pinned GitHub Actions step". Cited by name, not
# by list position: the numbering has already shifted once and every citation to it went stale silently.
# This is
# machine-maintained metadata, not prose \u2014 Dependabot rewrites the SHA and the annotation in one edit, so
# unlike an ordinary comment it cannot drift away from what it describes. Without it a workflow is a wall
# of 40-character hashes.
#
# The trailing `$` is what keeps this from becoming a carve-out: the comment must be a bare version and
# nothing else, so `@<sha> # v3 - revisit after the runner upgrade` still reports. Narrowing this arm any
# further than the allow-list entry would be weakening the detector, not teaching it.
$pinnedActionVersionPattern = '^\s*-?\s*uses:\s*\S+@[0-9a-f]{40}\s+#\s*v?[\w.\-]+\s*$'
# COMMENTS.md allow-list, "a line the toolchain parses rather than reads". `// @ts-check` turns on type checking,
# `#Requires` is a PowerShell engine directive that refuses to run the script. Neither is prose, so neither
# is a comment this policy has anything to say about — they were carried as "standing false positives" for
# a long time, which is a queue nobody can ever dispose of.
#
# Anchored at both ends on purpose: the moment a human appends prose, the line is doing two jobs and goes
# back to being an ordinary comment.
#
# `#Requires` takes its switch plus at most one value token (`-Version 7`, `-Modules Foo,Bar`,
# `-RunAsAdministrator`); PowerShell will not parse trailing prose, so refusing to match it is both
# stricter and truer to the language. An unusual hashtable form with internal spaces falls through and gets
# reported, which is the safe direction to fail.
$toolingDirectivePattern = '^\s*(//\s*@ts-(check|nocheck)|#Requires\s+-\w+(\s+\S+)?)\s*$'

$rows = New-Object System.Collections.Generic.List[object]

# A catalog reports WORK OWED. Neither of the two drops below can ever become an action, so emitting them
# made the row count unreadable and the zero unfalsifiable: a run reporting 12,975 rows of which zero were
# actionable reads identically to a scanner that matches everything, and a reader had to decompose the
# count against COMMENTS.md's prose to discover that. The allowance contract is "allowed shape silent or
# keep" - silence is the compliant half, and the ADJACENT violating shape is still reported, which is what
# pins each arm. The tooling-directive allowance was already silent with a bare `continue`; AAA markers,
# migration headers and suppression justifications were not, which is the inconsistency this removes.
#
# The classification arms are deliberately left intact and still call through here. Deleting their calls
# instead would let an allowed shape fall through to a violation category further down - a bare
# `// Arrange` would be reported as narrative - so the recognition must still happen and only the row is
# dropped.
function Add-Row($repo, $relPath, $lineNum, $lang, $category, $disposition, $snippet) {
    # A repo COMMENTS.md does not bind is out of scope, not exempt-and-listed: COMMENTS.md's own catalog
    # section forbids a standing false-positive list and a queue nobody may dispose of, which is exactly
    # what a rewritten disposition produced.
    if ($exemptRepos -contains $repo) { return }
    if ($disposition -eq 'keep') { return }

    $rows.Add([pscustomobject]@{
        Repo        = $repo
        Path        = $relPath
        Line        = $lineNum
        Language    = $lang
        Category    = $category
        Disposition = $disposition
        Snippet     = ($snippet.Trim() -replace '\s+', ' ')
    })
}

$stringLiteralPattern = '"(?:[^"\\]|\\.)*"|''(?:[^''\\]|\\.)*''|`(?:[^`\\]|\\.)*`'

function Remove-StringLiterals([string]$line) {
    return [regex]::Replace($line, $stringLiteralPattern, '')
}

function Scan-CStyle($file, $repo, $relPath, $lang) {
    # Strip /** ... */ and /* ... */ block regions first (multiline), remembering which were JSDoc (**) vs plain.
    $text = Get-Content -LiteralPath $file.FullName -Raw -ErrorAction SilentlyContinue
    if (-not $text) { return }
    $lines = $text -split "`r?`n"
    $inBlock = $false
    $blockIsDoc = $false
    for ($i = 0; $i -lt $lines.Count; $i++) {
        $line = $lines[$i]
        $lineNum = $i + 1

        if ($inBlock) {
            if ($line -match '\*/') { $inBlock = $false }
            if (-not $blockIsDoc -and $line.Trim() -ne '' -and $line -notmatch '^\s*\*') {
                Add-Row $repo $relPath $lineNum $lang 'block-comment' 'needs-triage' $line
            }
            continue
        }
        # A block that opens and closes on the same line must not leave $inBlock set: the close was only
        # ever tested on *subsequent* lines, so one `/** x */` made the whole rest of the file invisible to
        # every category below. That is why the fleet's TypeScript files reported zero rows.
        # Markers are matched against the line with string literals blanked out, so an ESLint glob like
        # files: ['**/*.ts'] is code rather than a block that swallows the rest of the file.
        $code = Remove-StringLiterals $line
        if ($code -match '/\*\*') {
            $blockIsDoc = $true
            $inBlock = $code -notmatch '\*/'
            Add-Row $repo $relPath $lineNum $lang 'jsdoc-block' 'needs-triage' $line
            continue
        }
        if ($code -match '/\*') {
            $blockIsDoc = $false
            $inBlock = $code -notmatch '\*/'
            # The fence row is unconditional. Emitting it only when the block also closed on the same line
            # meant a multi-line block whose body lines are *-prefixed (skipped above as decoration)
            # produced no rows at all — the starred-plain blind spot Test-CommentCatalog.ps1 pins.
            Add-Row $repo $relPath $lineNum $lang 'block-comment' 'needs-triage' $line
            continue
        }

        if ($line -match '^\s*///') {
            Add-Row $repo $relPath $lineNum $lang 'xml-doc' 'delete-or-migrate' $line
            continue
        }
        if ($line -match $toolingDirectivePattern) {
            continue
        }
        # The marker allowance is C# UNIT ONLY. The same three words are mandatory in a C# unit test and
        # noise in every other tier and every other language, so an unscoped allowance silenced them
        # everywhere and left the prohibition with no detector at all.
        if ($line -match $aaaBarePattern) {
            if ($lang -ne 'CSharp') {
                Add-Row $repo $relPath $lineNum $lang 'aaa-marker-outside-csharp' 'delete' $line
            }
            elseif ((Get-ScanArea -RelativePath $relPath) -eq 'test-unit') {
                Add-Row $repo $relPath $lineNum $lang 'aaa-marker' 'keep' $line
            }
            else {
                Add-Row $repo $relPath $lineNum $lang 'aaa-marker-outside-unit-tests' 'delete' $line
            }

            continue
        }
        if ($line -match $aaaNarrativePattern) {
            Add-Row $repo $relPath $lineNum $lang 'aaa-marker-with-narrative' 'trim-to-marker' $line
            continue
        }
        if ($line -match $suppressionPattern -and $line -match '(//|#)') {
            Add-Row $repo $relPath $lineNum $lang 'suppression-justification' 'keep' $line
            continue
        }
        if ($line -match $bannerPattern) {
            Add-Row $repo $relPath $lineNum $lang 'section-banner' 'delete' $line
            continue
        }
        if ($line -match '^\s*//(?!/)\S?') {
            Add-Row $repo $relPath $lineNum $lang 'narrative-or-restate' 'needs-triage' $line
            continue
        }
        if ($line -match ';\s*//(?!/)' -or $line -match '\}\s*//(?!/)') {
            Add-Row $repo $relPath $lineNum $lang 'trailing-comment' 'needs-triage' $line
        }
    }
}

function Scan-HashComment($file, $repo, $relPath, $lang, [switch]$StripPythonDocstrings, [string]$CommentToken = '#', [switch]$IsNumberedMigration) {
    $text = Get-Content -LiteralPath $file.FullName -Raw -ErrorAction SilentlyContinue
    if (-not $text) { return }
    $token = [regex]::Escape($CommentToken)
    # '#!' opens a shebang rather than a comment; '--' carries no such ambiguity.
    $leadingPattern = if ($CommentToken -eq '#') { '^\s*#(?!\!)' } else { "^\s*$token" }
    $trailingPattern = "\S.*\s$token\s\S"
    # Every arm that consumes the token derives from it here. The module-level $bannerPattern and the two
    # $aaa* ones hardcode '//|#', so under -CommentToken '--' they can never fire and SQL banners fall
    # through to narrative-or-restate.
    $tokenBanner = "^\s*$token\s*[─═—\-=]{3,}"
    $tokenAaaBare = "^\s*$token\s*(Arrange|Act|Assert|Given|When|Then)\s*$"
    $tokenAaaNarrative = "^\s*$token\s*(Arrange|Act|Assert|Given|When|Then)\b\s*[:—-]"
    if ($StripPythonDocstrings) {
        # Strip triple-quoted docstring regions (approximate — not string-literal-aware).
        $text = [regex]::Replace($text, '"""[\s\S]*?"""', { param($m) ($m.Value -split "`n" | ForEach-Object { '' }) -join "`n" })
        $text = [regex]::Replace($text, "'''[\s\S]*?'''", { param($m) ($m.Value -split "`n" | ForEach-Object { '' }) -join "`n" })
    }
    $lines = $text -split "`r?`n"
    # COMMENTS.md allow-list, "the header block of a numbered SQL migration". Scoped to the leading block of a NUMBERED migration, which is the
    # only file whose comment cannot drift: an applied migration is append-only and is never edited again.
    # Running commentary further down the file is ordinary narrative and still needs triage.
    $inMigrationHeader = $IsNumberedMigration
    for ($i = 0; $i -lt $lines.Count; $i++) {
        $line = $lines[$i]
        $lineNum = $i + 1
        if ($inMigrationHeader) {
            if ($line -match $leadingPattern) {
                Add-Row $repo $relPath $lineNum $lang 'migration-header' 'keep' $line
                continue
            }
            if ($line.Trim() -ne '') { $inMigrationHeader = $false }
        }
        if ($line -match $tokenAaaBare) {
            Add-Row $repo $relPath $lineNum $lang 'aaa-marker-outside-csharp' 'delete' $line
            continue
        }
        if ($line -match $tokenAaaNarrative) {
            Add-Row $repo $relPath $lineNum $lang 'aaa-marker-with-narrative' 'trim-to-marker' $line
            continue
        }
        if ($line -match $suppressionPattern) {
            Add-Row $repo $relPath $lineNum $lang 'suppression-justification' 'keep' $line
            continue
        }
        if ($line -match $tokenBanner) {
            # 'delete' is an instruction to edit the file. run_migrations.py keys on filename with no
            # checksum, so an edit to an applied migration neither re-applies nor gets detected - the file
            # silently stops matching the SQL that built the schema. Classify it, but route it to a human.
            $bannerDisposition = if ($IsNumberedMigration) { 'needs-triage' } else { 'delete' }
            Add-Row $repo $relPath $lineNum $lang 'section-banner' $bannerDisposition $line
            continue
        }
        if ($line -match $leadingPattern) {
            Add-Row $repo $relPath $lineNum $lang 'narrative-or-restate' 'needs-triage' $line
            continue
        }
        if ($lang -eq 'Yaml' -and $line -match $pinnedActionVersionPattern) {
            continue
        }
        if ($line -match $trailingPattern) {
            Add-Row $repo $relPath $lineNum $lang 'trailing-comment' 'needs-triage' $line
        }
    }
}

function Scan-Python($file, $repo, $relPath) {
    Scan-HashComment $file $repo $relPath 'Python' -StripPythonDocstrings
}

function Scan-PowerShell($file, $repo, $relPath) {
    $text = Get-Content -LiteralPath $file.FullName -Raw -ErrorAction SilentlyContinue
    if (-not $text) { return }
    $lines = $text -split "`r?`n"
    $inBlock = $false
    $blockIsHelp = $false
    for ($i = 0; $i -lt $lines.Count; $i++) {
        $line = $lines[$i]
        $lineNum = $i + 1

        if ($inBlock) {
            if ($line -match '#>') { $inBlock = $false }
            if (-not $blockIsHelp -and $line.Trim() -ne '') {
                Add-Row $repo $relPath $lineNum 'PowerShell' 'block-comment' 'needs-triage' $line
            }
            continue
        }
        if ($line -match '<#') {
            $inBlock = $true
            $blockIsHelp = ($lines[($i)..([Math]::Min($i + 15, $lines.Count - 1))] -join "`n") -match '\.SYNOPSIS|\.DESCRIPTION|\.EXAMPLE'
            continue
        }

        if ($line -match $toolingDirectivePattern) {
            continue
        }
        if ($line -match $aaaBarePattern) {
            if ((Get-ScanArea -RelativePath $relPath) -eq 'test-unit') {
                Add-Row $repo $relPath $lineNum 'PowerShell' 'aaa-marker' 'keep' $line
            }
            else {
                Add-Row $repo $relPath $lineNum 'PowerShell' 'aaa-marker-outside-unit-tests' 'delete' $line
            }

            continue
        }
        if ($line -match $aaaNarrativePattern) {
            Add-Row $repo $relPath $lineNum 'PowerShell' 'aaa-marker-with-narrative' 'trim-to-marker' $line
            continue
        }
        if ($line -match $suppressionPattern) {
            Add-Row $repo $relPath $lineNum 'PowerShell' 'suppression-justification' 'keep' $line
            continue
        }
        if ($line -match $bannerPattern) {
            Add-Row $repo $relPath $lineNum 'PowerShell' 'section-banner' 'delete' $line
            continue
        }
        if ($line -match '^\s*#(?!#)') {
            Add-Row $repo $relPath $lineNum 'PowerShell' 'narrative-or-restate' 'needs-triage' $line
        }
    }
}

Write-Host "Scanning $WorkspaceRoot ..."

$extMap = Get-ScanExtensionMap

$scanRoot = if ($RepoPath) { $RepoPath } elseif ($Repo) { Join-Path $WorkspaceRoot $Repo } else { $WorkspaceRoot }
if (-not (Test-Path -LiteralPath $scanRoot -PathType Container)) { throw "Repo '$Repo' is not a directory under $WorkspaceRoot." }
$files = Get-ChildItem -Path $scanRoot -Recurse -File -ErrorAction SilentlyContinue |
    Where-Object { $extMap.ContainsKey($_.Extension) -and -not (Test-Excluded $_) }

$total = $files.Count
$n = 0
foreach ($f in $files) {
    $n++
    if ($n % 200 -eq 0) { Write-Host "  $n / $total files scanned..." }
    $repo = Get-RepoName $f.FullName $WorkspaceRoot
    $rel = $f.FullName.Substring($WorkspaceRoot.Length).TrimStart('\')
    $lang = $extMap[$f.Extension]
    switch ($lang) {
        'Python' { Scan-Python $f $repo $rel }
        'PowerShell' { Scan-PowerShell $f $repo $rel }
        'Shell' { Scan-HashComment $f $repo $rel 'Shell' }
        'Sql' { Scan-HashComment $f $repo $rel 'Sql' -CommentToken '--' -IsNumberedMigration:($rel -match '[\\/]migrations[\\/]\d{4}_') }
        'Yaml' { Scan-HashComment $f $repo $rel 'Yaml' }
        default { Scan-CStyle $f $repo $rel $lang }
    }
}

$outDir = Split-Path -Parent $OutputPath
if (-not (Test-Path $outDir)) { New-Item -ItemType Directory -Path $outDir -Force | Out-Null }
$rows | Sort-Object Repo, Path, Line | Export-Csv -Path $OutputPath -NoTypeInformation -Encoding UTF8

Write-Host "Scanned $total files. Wrote $($rows.Count) rows to $OutputPath"
$rows | Group-Object Repo, Category | Sort-Object Name | Select-Object Name, Count | Format-Table -AutoSize

<#
.SYNOPSIS
    Regression harness for Get-CommentCatalog.ps1.

.DESCRIPTION
    Builds a synthetic fixture workspace and asserts both directions per category: the lines that must be
    flagged, and the lines that must not. Fixtures rather than the live repos, deliberately - an assertion
    like "Librarian returns exactly 44 rows" rots the moment anyone edits a component.

    Run this after editing the scanner, and break the scanner on purpose to watch a check go red before
    trusting it. The counterpart for Tools/AGENTS/CODE-STYLE.md is Tools/Claude/Test-CodeStyleCatalog.ps1; this
    script is the Tools/AGENTS/COMMENTS.md half, which had no harness until the one-line-block defect below.

.EXAMPLE
    pwsh -File "C:\path\to\Test-CommentCatalog.ps1"
#>
[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'

$scanner = Join-Path $PSScriptRoot 'Get-CommentCatalog.ps1'
if (-not (Test-Path -LiteralPath $scanner)) { throw "Scanner not found beside this harness: $scanner" }

. "$PSScriptRoot\..\Hooks\ClaudeTemp.ps1"
$root = New-ClaudeTempPath "comment-catalog-harness-$([guid]::NewGuid())"
$outputPath = Join-Path $root 'catalog.csv'

function New-Fixture([string]$relativePath, [string]$content) {
    $full = Join-Path $root $relativePath
    $dir = Split-Path -Parent $full
    if (-not (Test-Path $dir)) { New-Item -ItemType Directory -Path $dir -Force | Out-Null }
    Set-Content -LiteralPath $full -Value $content -Encoding UTF8
}

# The defect this harness was written for: a block that opens AND closes on one line used to leave the
# scanner in "inside a block" state forever, so everything below it in the file became invisible.
New-Fixture 'Librarian/src/one-line-doc.ts' @'
/** A one-line doc block. */
export const x = 1;
// This narrative comment sits below the one-line block and must still be reported.
'@

New-Fixture 'Librarian/src/one-line-plain.ts' @'
/* A one-line plain block. */
export const y = 2;
'@

New-Fixture 'Librarian/src/multi-line-plain.ts' @'
/*
narrative line inside a plain block
*/
export const z = 3;
'@

New-Fixture 'Librarian/src/multi-line-doc.ts' @'
/**
 * A JSDoc block spanning several lines.
 */
export const w = 4;
'@

# The starred-plain blind spot: a plain /* block whose body lines are *-prefixed used to emit no rows at
# all — the opener only produced a row when the block also closed on the same line, and *-prefixed body
# lines are skipped as block decoration. The fence row is the repair; the body stays un-itemized.
New-Fixture 'Librarian/src/starred-plain.css' @'
/*
 * A starred narrative line the fence row must now represent.
 */
.a { color: red; }
'@

# An ESLint glob is code, not a block comment. Before the string-literal blanking these lines set the
# scanner "inside a block" and every category below them went silent for the rest of the file.
New-Fixture 'Librarian/src/globs.js' @'
export default [
  { files: ['**/*.ts'] },
  { files: ['**/*.spec.ts'] },
];
// This narrative comment sits below the globs and must still be reported.
/* A real block comment must still be reported even on a line that also carries a string. */
'@

New-Fixture 'Identity/Identity.Tests.Unit/Markers.cs' @'
public class Markers
{
    public void Case()
    {
        // Arrange
        var a = 1;
        // Act + Assert
        var b = a;
#pragma warning disable CA1034 // roslyn-analyzers#7765: false positive
        var c = b;
#pragma warning restore CA1034
        var d = c; // trailing narrative
    }

    /// <summary>Doc.</summary>
    public int E { get; set; }
}
'@

New-Fixture 'Identity/Identity/StrykerDisable.cs' @'
public class StrykerDisable
{
    public bool Reasoned(int id)
    {
        // Stryker disable once Equality : id is an identity column, never 0, so >= and > select the same rows
        return id > 0;
    }

    public bool Bare(int id)
    {
        // Stryker disable once Equality
        return id > 0;
    }

    public bool All(int id)
    {
        // Stryker disable all : every mutant in this file
        return id > 0;
    }
}
'@

# The bare-marker allowance is scoped to unit tests, so the SAME three words must report here. This
# fixture is the control of opposite sign for Markers.cs: without it, an allowance that silenced markers
# everywhere would look identical to one correctly scoped.
#
# Named E2ePhases rather than E2EMarkers because Assert-Row and Assert-NoRow match a path FRAGMENT: a
# basename ending in 'Markers.cs' is selected by the 'Markers.cs' needle as well, and the unit-test
# assertion then reads this file's row and fails against a correct scanner.
New-Fixture 'Identity/Identity.Tests.E2E/E2ePhases.cs' @'
public class E2ePhases
{
    public void Case()
    {
        // Arrange
        var a = 1;
    }
}
'@

# Integration is INSIDE the prohibition: markers are unit-only. This control is the one that would
# otherwise go live silently - no project carries a `.Tests.Integration` name today, so an allowance
# widened to Integration agrees with the ruling by accident until the first rename, and then reverses.
New-Fixture 'Identity/Identity.Tests.Integration/IntegrationPhases.cs' @'
public class IntegrationPhases
{
    public void Case()
    {
        // Arrange
        var a = 1;
    }
}
'@

# The markers are a C# convention: a TypeScript or Python test carries none, in any tier. The C# unit
# test in Markers.cs is the control of opposite sign, where the bare marker stays silent.
New-Fixture 'Churches/integration/IntegrationPhases.spec.ts' @'
test('case', async () => {
  // Arrange
  const a = 1;
});
'@

New-Fixture 'Churches/src/UnitPhases.spec.ts' @'
it('case', () => {
  // Arrange
  const a = 1;
});
'@

New-Fixture 'Curator/tests/test_phases.py' @'
def test_case():
    # Arrange
    a = 1
'@

New-Fixture 'Identity/src/Banner.cs' @'
public class Banner
{
    // ─────────────────────────
    public int F { get; set; }
}
'@

New-Fixture 'Tools/exempt.ps1' @'
# A narrative comment in an exempt repo.
$a = 1
'@

# SQL migrations live in a directory named 'migrations', and -contains is case-insensitive, so the
# EF Core 'Migrations' exclusion used to hide every one of them even once '.sql' was mapped.
New-Fixture 'Curator/db/migrations/0001_thing.sql' @'
-- Adds the thing.
-- Second header line, still part of the same block.
ALTER TABLE games ADD COLUMN thing text;
-- Running commentary below the first statement is not a header.
ALTER TABLE games ADD COLUMN other text; -- inline note
-- ============================================================
-- Section below the header
-- ============================================================
'@

# An unnumbered .sql file is not append-only, so it gets no header exemption.
New-Fixture 'Curator/db/seed.sql' @'
-- Not a numbered migration.
INSERT INTO games DEFAULT VALUES;
-- ------------------------------
'@

# The other direction: EF Core scaffolds C# migrations, and their generator prose stays excluded.
New-Fixture 'Identity/Migrations/20240101_Init.cs' @'
public class Init
{
    // Generator narrative that must stay invisible.
}
'@

# CSS has one comment form and no test can carry its 'what', so every block is a triage row.
New-Fixture 'Librarian/src/styles.css' @'
/* Buttons */
.btn-primary {
  color: red;
}
/* A cataloged work's title - italicized the way a card catalog italicizes a title. */
.catalog-title {
  font-style: italic;
}
.thing {
  /* stylelint-disable-next-line declaration-property-value-allowed-list -- eslint-disable style tooling artifact */
  z-index: 3;
}
'@

# Minified CSS is generator output; its licence header must not become a row.
New-Fixture 'Identity/wwwroot/css/tabler.min.css' @'
/*! Tabler v1.0 | @license MIT */
.a{color:red}
'@

# COMMENTS.md allow-list, "the version annotation on a SHA-pinned GitHub Actions step" (cited by name --
# the list has renumbered before). The allowance is narrow on purpose and these rows are what keeps it
# narrow: only a SHA-pinned `uses:` annotated with a bare version is silent. A version with prose after it,
# a tag pin rather than a SHA, and an ordinary trailing comment all stay reported, so the arm cannot decay
# into a blanket exemption for workflow comments.
New-Fixture 'Librarian/.github/workflows/pinned.yml' @'
steps:
  - uses: actions/checkout@11bd71901bbe5b1630ceea73d27597364c9af683 # v4.2.2
  - uses: azure/login@f5d393ae46f8fde4be8b75f32e3fc50e654ad0ca # v3.0.1 - revisit after the runner upgrade
  - uses: actions/setup-node@v4 # v4
    run: npm ci # install everything
'@

# COMMENTS.md allow-list, "a line the toolchain parses rather than reads". A line the toolchain parses is
# not prose; a line that parses AND carries
# prose is back to being an ordinary comment, which is what the second row of each pair pins.
New-Fixture 'crgolden.com/directives.mjs' @'
// @ts-check
// @ts-check because the config kept drifting
export default {};
'@

# Modules compiles src/ a second time into dist-test/ for its test run, so every authored comment there
# was counted twice and the copy cannot be fixed - the next build rewrites it. The third fixture is the
# control that keeps the exclusion an exact name: a directory merely starting with 'dist' is authored
# source and must stay reported, the same trap the Migrations entry above records.
New-Fixture 'Modules/src/mirrored.ts' @'
// An authored narrative comment in source.
export const a = 1;
'@

New-Fixture 'Modules/dist-test/mirrored.js' @'
// The same comment, compiled into the test build output.
exports.a = 1;
'@

New-Fixture 'Modules/src/distribution/authored.ts' @'
// A directory whose name merely starts with dist is authored source.
export const b = 2;
'@

New-Fixture 'Inventory/client/playwright-report/trace/assets/viewer.js' @'
// A comment inside Playwright's generated HTML report bundle.
export const c = 3;
'@

New-Fixture 'Inventory/client/playwright-reporting/authored.ts' @'
// A directory whose name merely starts with playwright-report is authored source.
export const d = 4;
'@

New-Fixture 'Inventory/client/cucumber-report/assets/report.js' @'
// A comment inside the generated Cucumber HTML report bundle.
export const e = 5;
'@

New-Fixture 'Inventory/client/cucumber-reporting/authored.ts' @'
// A directory whose name merely starts with cucumber-report is authored source.
export const f = 6;
'@

New-Fixture 'Inventory/client/.features-gen/chromium/e2e/features/generated.feature.spec.js' @'
// Generated from: e2e/features/generated.feature
export const g = 7;
'@

New-Fixture 'Inventory/client/features-gen-notes/authored.ts' @'
// A directory named like features-gen without the leading dot is authored source.
export const h = 8;
'@

# Deliberately NOT under Tools/: that repo is in $exemptRepos, which rewrites every disposition to
# 'exempt' and would hide whether this arm actually fired.
New-Fixture 'Identity/directives.ps1' @'
#Requires -Version 7
#Requires -Version 7 so the ternary works
Write-Output 'x'
'@

& $scanner -WorkspaceRoot $root -OutputPath $outputPath | Out-Null
$rows = Import-Csv -LiteralPath $outputPath

function Get-Rows([string]$pathFragment) {
    return @($rows | Where-Object { $_.Path -like "*$pathFragment*" })
}

$failures = New-Object System.Collections.Generic.List[string]
$checks = 0

function Assert-Row([string]$name, [string]$pathFragment, [int]$line, [string]$category, [string]$disposition) {
    $script:checks++
    $match = @(Get-Rows $pathFragment | Where-Object {
            [int]$_.Line -eq $line -and $_.Category -eq $category -and $_.Disposition -eq $disposition
        })
    if ($match.Count -ne 1) {
        $seen = (Get-Rows $pathFragment | ForEach-Object { "$($_.Line):$($_.Category)/$($_.Disposition)" }) -join ', '
        $failures.Add("MUST FLAG - $name : expected line $line as $category/$disposition; saw [$seen]")
    }
}

function Assert-NoRow([string]$name, [string]$pathFragment, [int]$line) {
    $script:checks++
    $match = @(Get-Rows $pathFragment | Where-Object { [int]$_.Line -eq $line })
    if ($match.Count -ne 0) {
        $seen = ($match | ForEach-Object { "$($_.Category)/$($_.Disposition)" }) -join ', '
        $failures.Add("MUST NOT FLAG - $name : line $line was reported as [$seen]")
    }
}

Assert-NoRow 'a bare @ts-check directive is not a comment'                  'directives.mjs' 1
Assert-Row   'a @ts-check line carrying prose is an ordinary comment'       'directives.mjs' 2 'narrative-or-restate' 'needs-triage'
Assert-NoRow 'a bare #Requires directive is not a comment'                  'directives.ps1' 1
Assert-Row   'a #Requires line carrying prose is an ordinary comment'       'directives.ps1' 2 'narrative-or-restate' 'needs-triage'

Assert-NoRow 'a SHA-pinned action with a bare version tag is allow-listed'  'pinned.yml' 2
Assert-Row   'a version tag carrying prose is still a trailing comment'     'pinned.yml' 3 'trailing-comment' 'needs-triage'
Assert-Row   'a tag pin is not a SHA pin and stays reported'                'pinned.yml' 4 'trailing-comment' 'needs-triage'
Assert-Row   'an ordinary trailing comment in a workflow stays reported'    'pinned.yml' 5 'trailing-comment' 'needs-triage'

Assert-Row   'a one-line doc block does not swallow the rest of the file' 'one-line-doc.ts' 3 'narrative-or-restate' 'needs-triage'
Assert-Row   'a one-line plain block is reported'                          'one-line-plain.ts' 1 'block-comment' 'needs-triage'
Assert-Row   'a multi-line plain block reports its body'                   'multi-line-plain.ts' 2 'block-comment' 'needs-triage'
Assert-Row   'a multi-line plain block reports its opening fence'          'multi-line-plain.ts' 1 'block-comment' 'needs-triage'
Assert-Row   'a starred plain block is visible via its fence row'          'starred-plain.css' 1 'block-comment' 'needs-triage'
Assert-NoRow 'a starred body line is not itemized apart from its fence'    'starred-plain.css' 2
Assert-Row   'a multi-line JSDoc block is reported'                        'multi-line-doc.ts' 1 'jsdoc-block' 'needs-triage'
Assert-NoRow 'a JSDoc body line is not reported separately from its block' 'multi-line-doc.ts' 2
Assert-Row   'a one-line JSDoc block is reported'                          'one-line-doc.ts' 1 'jsdoc-block' 'needs-triage'
# An allowed shape is SILENT, which is the compliant half of COMMENTS.md's "silent or keep" contract. The
# adjacent violating shape on the line after it is what pins the arm: if the scanner had simply stopped
# matching, the combined marker and the trailing comment below would go quiet too.
Assert-NoRow 'a bare AAA marker is silent in a unit test'                  'Markers.cs' 5
Assert-Row   'a bare AAA marker is reported in an integration test'        'IntegrationPhases.cs' 5 'aaa-marker-outside-unit-tests' 'delete'
Assert-Row   'the same marker is reported outside unit tests'              'E2ePhases.cs' 5 'aaa-marker-outside-unit-tests' 'delete'
Assert-Row   'a bare AAA marker is reported in a TypeScript integration spec' 'IntegrationPhases.spec.ts' 2 'aaa-marker-outside-csharp' 'delete'
Assert-Row   'a bare AAA marker is reported in a TypeScript unit spec'    'UnitPhases.spec.ts' 2 'aaa-marker-outside-csharp' 'delete'
Assert-Row   'a bare AAA marker is reported in a Python test'             'test_phases.py' 2 'aaa-marker-outside-csharp' 'delete'
Assert-Row   'a combined AAA marker stays flagged'                         'Markers.cs' 7 'narrative-or-restate' 'needs-triage'
Assert-NoRow 'a suppression justification is silent'                       'Markers.cs' 9
Assert-NoRow 'a one-line Stryker disable naming its mutator and reason is silent' 'StrykerDisable.cs' 5
Assert-Row   'a Stryker disable with no reason is reported'                'StrykerDisable.cs' 11 'narrative-or-restate' 'needs-triage'
Assert-Row   'a Stryker disable all is reported, reason or not'           'StrykerDisable.cs' 17 'narrative-or-restate' 'needs-triage'
Assert-Row   'a trailing comment is reported'                              'Markers.cs' 12 'trailing-comment' 'needs-triage'
Assert-Row   'XML doc is migrate-or-delete'                                'Markers.cs' 15 'xml-doc' 'delete-or-migrate'
Assert-Row   'a section banner is delete'                                  'Banner.cs' 3 'section-banner' 'delete'
# A repo COMMENTS.md does not bind produces NO ROW, rather than a row relabelled 'exempt'. The control of
# opposite sign is 'a glob does not silence the rest of the file' two lines down: the identical narrative
# shape in a bound repo still reports, so silence here is scope and not the scanner having gone quiet.
Assert-NoRow 'a repo outside the policy scope produces no row'             'exempt.ps1' 1
Assert-NoRow 'a /* inside a string literal is not a block comment'         'globs.js' 3
Assert-Row   'a glob does not silence the rest of the file'                'globs.js' 5 'narrative-or-restate' 'needs-triage'
Assert-Row   'a real block comment beside a string is still reported'      'globs.js' 6 'block-comment' 'needs-triage'
# COMMENTS.md allow-list, "the header block of a numbered SQL migration": silent, because an applied
# migration is append-only and cannot drift. Everything past the first statement is ordinary narrative,
# and the two rows below are what prove the header arm stopped at the first statement rather than
# silencing the file.
Assert-NoRow 'a SQL migration header is silent'                           '0001_thing.sql' 1
Assert-NoRow 'the whole leading block is silent, not just line 1'         '0001_thing.sql' 2
Assert-Row   'a comment below the first statement still needs triage'     '0001_thing.sql' 4 'narrative-or-restate' 'needs-triage'
Assert-Row   'a trailing SQL comment is reported'                         '0001_thing.sql' 5 'trailing-comment' 'needs-triage'
Assert-NoRow 'SQL code without a comment is not reported'                 '0001_thing.sql' 3
Assert-Row   'an unnumbered .sql file gets no header exemption'           'seed.sql' 1 'narrative-or-restate' 'needs-triage'
# $bannerPattern hardcodes '//|#', so before the token-derived patterns a '--' banner could not reach the
# banner arm at all and landed as narrative. These two pin both directions: classified either way, but
# only the unnumbered file is told to delete, since editing an applied migration is undetectable.
Assert-Row   'a SQL banner is classified as a banner, not narrative'     '0001_thing.sql' 6 'section-banner' 'needs-triage'
Assert-Row   'prose between two banners is still narrative'              '0001_thing.sql' 7 'narrative-or-restate' 'needs-triage'
# CSS was absent from $extMap until 2026-08-25, so a full Tailwind redesign reported zero rows in the one
# language it was about. These pin both directions: authored CSS prose is a row, generator output is not.
Assert-Row   'a CSS banner divider is a row'                             'styles.css' 1 'block-comment' 'needs-triage'
Assert-Row   'a CSS design-thesis comment is a row'                      'styles.css' 5 'block-comment' 'needs-triage'
Assert-NoRow 'a CSS declaration without a comment is not a row'          'styles.css' 2
Assert-NoRow 'minified vendored CSS is excluded entirely'                'tabler.min.css' 1
Assert-Row   'a banner in an unnumbered .sql is delete'                  'seed.sql' 3 'section-banner' 'delete'
Assert-NoRow 'a scaffolded C# migration stays excluded'                   '20240101_Init.cs' 3
Assert-Row   'an authored comment in src is a row'                       'src\mirrored.ts' 1 'narrative-or-restate' 'needs-triage'
Assert-NoRow 'its compiled copy under dist-test is excluded'             'dist-test\mirrored.js' 1
Assert-Row   'a directory merely starting with dist stays reported'      'distribution\authored.ts' 1 'narrative-or-restate' 'needs-triage'
Assert-NoRow 'a generated Playwright report bundle is excluded'          'playwright-report\trace\assets\viewer.js' 1
Assert-Row   'a directory merely starting with playwright-report stays reported' 'playwright-reporting\authored.ts' 1 'narrative-or-restate' 'needs-triage'
Assert-NoRow 'a generated Cucumber report bundle is excluded'            'cucumber-report\assets\report.js' 1
Assert-Row   'a directory merely starting with cucumber-report stays reported' 'cucumber-reporting\authored.ts' 1 'narrative-or-restate' 'needs-triage'
Assert-NoRow 'a generated playwright-bdd spec is excluded'               '.features-gen\chromium\e2e\features\generated.feature.spec.js' 1
Assert-Row   'a features-gen directory without the dot stays reported'   'features-gen-notes\authored.ts' 1 'narrative-or-restate' 'needs-triage'

$checks++
$sqlLanguages = @(Get-Rows '0001_thing.sql' | ForEach-Object { $_.Language } | Sort-Object -Unique)
if ($sqlLanguages.Count -ne 1 -or $sqlLanguages[0] -ne 'Sql') {
    $failures.Add("LANGUAGE - SQL rows should carry Language='Sql'; saw [$($sqlLanguages -join ', ')]")
}

Remove-FixtureTree $root

if ($failures.Count -gt 0) {
    Write-Host "`nFAIL: $($failures.Count) check(s)." -ForegroundColor Red
    foreach ($failure in $failures) { Write-Host "  $failure" -ForegroundColor Red }
    exit 1
}

Write-Host "`nOK: $checks checks passed." -ForegroundColor Green

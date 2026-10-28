<#
.SYNOPSIS
    Catalogs Tools/AGENTS/CODE-STYLE.md rule violations across every repo in the crgolden workspace.

.DESCRIPTION
    The CODE-STYLE.md counterpart to Tools/CommentAudit/Get-CommentCatalog.ps1. Walks each independent
    repo under the workspace root and emits one CSV row per candidate violation of a numbered crgolden
    rule, tagged with the rule number, a category, and a Disposition naming what should happen to it.

    THE COMPILER IS THE PREFERRED ENFORCEMENT LAYER. THIS SCRIPT IS THE FALLBACK.
    Before extending this script, check CODE-STYLE.md's measured coverage table ("Which rules the compiler
    can take, and which it cannot"). An analyzer has the semantic model - it knows whether `prefix` is a
    string or a char, and whether a Where clause is IQueryable - and it fails the build on the exact call
    site instead of adding a row someone has to triage. Rule 8 is mostly CA1307/CA1309/CA1310/CA1862;
    rule 2's Uri case is CA1054/CA1055/CA1056; rules 3, 5 and 13 are better served by
    Microsoft.CodeAnalysis.BannedApiAnalyzers (RS0030), whose message can name the crgolden rule directly.
    What genuinely has no analyzer, verified against deliberate violations rather than assumed: rules 1,
    4, 9, 10, 12, and the dominant shape of 11. Grow this script only there.

    Like the comment catalog, this is a heuristic regex pass, not a language parser, and it does NOT
    decide legitimacy on its own. Ambiguity is expressed in the Disposition column rather than hidden in
    the regex, so the output is a triage queue with a legible breakdown instead of a single violation
    count that has to be caveated in prose. A second run confirms nothing new appeared.

    Rules covered, and how far each can honestly be taken mechanically:

      rule 1  no empty strings          - the four positions where an empty string is the BOUND VALUE or
                                          the RETURN: `?? x`, `= x;`, `return x;`, `=> x`. The only rule
                                          that runs on TypeScript as well as C#, via Scan-TypeScriptLine
                                          (single, double and backtick quotes). "TypeScript" here means
                                          `.ts`, `.tsx`, `.mts` and `.cts` ONLY - see the $scanExtensions note below for
                                          the `.js`/`.mjs`/`.cjs` files that are consequently unscanned.
                                          An empty string in
                                          call-argument position - `parts.join('')`, `word.split('')`,
                                          `value.replace(/x/g, '')` - is idiomatic JS for "no separator"
                                          and is deliberately NOT matched.
      rule 2  strongest type            - A `string` property or parameter reports only on evidence: the
                                          repo's own production code converting it with a throwing
                                          conversion (Parse, new Uri, Enum.Parse<T>, Convert.To*), as
                                          use-<type>. TryParse is a boundary and does not count. A name is
                                          not evidence; an id someone else mints stays a string. `*Url`
                                          and `*Uri` strings mirror CA1056 (use-uri).
                                          The INVERSE case is emitted too: a `Uri` auto-property, because
                                          rule 2 stops at a schema-declaring serializer. EDM has a closed
                                          primitive set and no model-builder API maps a non-primitive CLR
                                          type onto one, so a `Uri` on an OData entity becomes a complex
                                          type carrying only `Segments` - lossy on read, undeserializable
                                          on write. That shipped and 400'd every PATCH in production while
                                          the BSON round-trip tests stayed green. Also needs-triage: the
                                          detector cannot tell an EDM entity from a plain POCO, so the row
                                          asks a human to read the declared schema. See CODE-STYLE.md
                                          "Where rule 2 stops".
      rule 3  DateTimeOffset in UTC     - `DateTime` declarations, `DateTime.Now/UtcNow`, `GetDateTime(`,
                                          `DateTimeKind`.
      rule 4  no nullable collections   - `List<T>?`, `IEnumerable<T>?`, `T[]?` and friends.
      rule 5  IsNullOrWhiteSpace        - `string.IsNullOrEmpty(` and bare `IsNullOrEmpty(` via
                                          `using static System.String`. Deliberately does NOT match
                                          `someRedisValue.IsNullOrEmpty`, which is StackExchange.Redis'
                                          unrelated RedisValue property.
      rule 6  comments                  - delegated to Get-CommentCatalog.ps1. Not duplicated here.
      rule 7  vertical slices           - architectural; no regex can decide it. Not scanned.
      rule 8  ordinal string comparison - `== "` / `!= "`, comparison against `string.Empty`, and every
                                          comparison API that does not state a StringComparison. Split by
                                          WHY: use-ordinal for the methods that are culture-sensitive BY
                                          DEFAULT (StartsWith/EndsWith/IndexOf/LastIndexOf with a string
                                          argument, string.Compare, ToLower()/ToUpper()) which are latent
                                          locale bugs; state-ordinal for the ones that are already ordinal
                                          but unstated (two-arg string.Equals, Contains, Replace).
                                          CompareTo gets its own use-string-compare-ordinal because it has
                                          no StringComparison overload to add.
                                          The char overloads - StartsWith('{'), IndexOf('?') - are ordinal
                                          already and are deliberately NOT flagged; the pattern requires a
                                          blanked STRING literal, which a char literal never produces.
                                          Ordering/set operations (OrderBy, Distinct, GroupBy,
                                          ToDictionary, ...) are needs-triage, never a directive: over
                                          LINQ-to-Objects ordering uses CurrentCulture and the equality
                                          operators are ordinal but unstated, but the same expression
                                          over an EF IQueryable becomes SQL sorted by the database
                                          collation, and the element type may not be a string at all.
      rule 9  no null-forgiving         - `x!.`, `x!,`, `x!)`, `x!;`, and a bang on a call or index
                                          result (`GetString()!`, `items[0]!`). Runs on C# AND
                                          TypeScript. Never matches `!=`, `!==`, `!flag` or `!(cond)`.
      rule 10 E2E selectors             - positional/presentational selectors in E2E suites. Selectors
                                          against third-party-generated DOM (Leaflet, etc.) are emitted
                                          as third-party-dom rather than use-id, because the app never
                                          authors those nodes; that row is the evidence for whether
                                          CODE-STYLE.md needs an explicit exception.
                                          copy-selector covers Playwright's getBy* family, which binds to
                                          copy the way rule 10's named `has-text(...)` does. It is its own
                                          category because every other rule-10 pattern requires a literal
                                          locator/querySelector call and so could not see it at all - a
                                          Librarian e2e spec held five such selectors while the catalog
                                          reported zero rows for that file, twice. The same shape recurred
                                          with Playwright's hasText option: the detector matched only the
                                          C# `HasText =` spelling, so the TypeScript `{ hasText: ... }`
                                          form was invisible and Librarian scored zero rule-10 rows with
                                          seven of them in one spec. A rule-10 count that falls to zero is
                                          a claim about the DETECTOR before it is a claim about the code.
                                          A bare getByRole with
                                          no `name` is NOT flagged (it selects a landmark, not copy), and
                                          neither is getByTestId (a durable authored hook). Note the line
                                          basis: a getByRole whose `name` sits on a continuation line is
                                          missed, as everywhere else in this heuristic pass.
      rule 11 generated unit-test data  - literals in unit/integration test projects, split into
                                          generate / move-to-sut-constant / keep-kat / needs-triage.
                                          Also the naming half: extract-to-named-local for a generator
                                          inlined into an argument (nothing can assert on an unnamed
                                          value) and rename-local for one parked in a placeholder name
                                          like `guid`/`id`/`value`, which names the type rather than the
                                          purpose. A generator that IS the bound value - heading the
                                          right-hand side, or interpolated into it - is compliant and
                                          emits nothing; an earlier version only accepted the first
                                          form and so charged authors for complying.
                                          A `"""` raw-string block is reported as ONE raw-string-block
                                          needs-triage row at its opening fence and its contents are
                                          not swept: the scan is line-based, and "generating" the 2/3/4
                                          in a `VALUES ($1, $2, $3, $4)` line destroys the statement.
                                          Numeric boundaries (0, 1, -1, int.MaxValue/MinValue) are
                                          dropped rather than emitted - they are rule 11's keep-boundary
                                          case and would swamp the queue with loop counters.

      rule 12 no logic in unit tests    - if/else/switch/loops/try-catch/ternary inside a [Fact] or
                                          [Theory] body. Tracks brace depth from each test method's
                                          opening brace so a named helper (which TESTING.md explicitly
                                          allows to branch) and DisposeAsync teardown are not flagged.
                                          Ternary is the imprecise one - nullable annotations, `?.` and
                                          `??` share the character - so it emits needs-triage, not a
                                          directive. `??` itself is not detected; treat rule 12's `??`
                                          clause as doc guidance a human applies.
                                          Second arm: a [MemberData] source whose rows read a generator,
                                          directly or through a static readonly field the same file
                                          initializes from one, reports once at its declaration as
                                          generated-theory-data-in-<Name> / split-into-facts. Such rows
                                          are conditions named by a random value, and xUnit v3 rebuilds
                                          their test ids per process, which leaves Stryker mutants Pending.
                                          A field initialized on a later line than its declaration, or
                                          in another file, is not seen.

      rule 13 no ad-hoc logging         - ILogger/ILoggerFactory dependencies, Log* calls, and
                                          Console.Write*. A Log* call that names an exception gets
                                          record-on-activity (Telemetry.Tracing.RecordHandledException)
                                          rather than a plain removal, since the exception still needs
                                          to reach a span. A test's ILogger is the same removal as the
                                          production one, just sequenced after it - the end state is
                                          zero ILogger dependencies fleet-wide, exception handler
                                          included, since HandleException uses a span and a metric
                                          rather than a log line.
                                          NOT detected: whether a call site is one of rule 13's
                                          documented exemptions. Cross-check hits against OBSERVABILITY.md
                                          before deleting anything.

      rule 14 no authored CSS           - every declaration in a .css file, a <style> block or a static
                                          style="" attribute (.html/.cshtml/.razor/.astro), and an Angular
                                          component's inline `styles:`. Exempt in code only: @theme and
                                          @plugin (daisyUI theme) blocks, @font-face, top-level at-rule
                                          statements (@import, @source), a prefers-color-scheme :root
                                          re-binding of a name the file's @theme defines, and an
                                          @layer base rule whose every selector is a type selector.
                                          Vendored wwwroot\lib\ is third-party and not scanned.

      rule 17 no manual timeouts        - every Area except prod, C# and TypeScript. per-call-timeout is a
                                          timeout argument or option, a named constant exactly like a
                                          literal; raised-default-timeout is setDefaultTimeout,
                                          test.setTimeout/slow, and the per-action default keys in a
                                          Playwright or Vitest config; wall-clock-sleep is waitForTimeout,
                                          Thread.Sleep, a Task.Delay with no TimeProvider, or a Promise
                                          around setTimeout; timeout-parameter is a helper accepting one.
                                          A bare `timeout:` in a config is the runner's whole-test or
                                          server-start budget and does not report.

    Rows are per finding, not per line, so one line can appear under two rules. 49 of the rule-8 rows sit
    on a line that also carries a rule-11 row (`a.Key == "denomination"` is both a `==` string comparison
    and a magic literal). Those are two independent defects, not a double count - generating the literal
    leaves the `==` still needing string.Equals - but do not add the per-rule totals and read the sum as
    a count of distinct lines. Only 10 rule-8 rows are Area=prod; filter on Area to see that.

    Scope note. The two catalogs share the extension map, via ScanScopeHelper.ps1, and exempt the same
    private ops repos ('Tools', 'psnpy'): an exempt repo produces no row at all. Test-CodeStyleCatalog.ps1
    fails when this script's $exemptRepos and Get-CommentCatalog.ps1's differ.

.PARAMETER WorkspaceRoot
    Root directory containing the independent per-repo git checkouts. Defaults to two levels above this
    script's own location (Tools/Claude/), which resolves to the workspace root regardless of which
    machine or user profile it runs from. Throws if the resolved path doesn't exist rather than silently
    scanning zero files.

.PARAMETER OutputPath
    Where to write the CSV. Defaults to code-style-catalog.csv under %TEMP%\claude (CLAUDE_CODE_TMPDIR when set),
    outside any synced or repo directory.

.PARAMETER Rule
    Optional rule-number filter, e.g. -Rule 5,11. Defaults to every implemented rule.

.PARAMETER Repo
    Scan one repo under the workspace root instead of all of them; repo names still resolve against the
    workspace root. With -RepoPath it is the name that repo's files carry, whatever its folder is called.

.PARAMETER RepoPath
    Scan this one checkout, wherever it is, as the repo -Repo names (its folder name when -Repo is absent).
    A repo's gate.ps1 passes both through GateCommon's Invoke-CatalogSteps, so a worktree or a clone under
    another folder name is scanned and exempted as the repo it is.

.EXAMPLE
    .\Get-CodeStyleCatalog.ps1

.EXAMPLE
    .\Get-CodeStyleCatalog.ps1 -Rule 11 -OutputPath 'C:\scratch\rule11.csv'
#>
[CmdletBinding()]
param(
    [string]$WorkspaceRoot = (Split-Path -Parent (Split-Path -Parent $PSScriptRoot)),
    [string]$OutputPath,
    [int[]]$Rule = @(1, 2, 3, 4, 5, 8, 9, 10, 11, 12, 13, 14, 17, 18),
    [string]$Repo,
    [string]$RepoPath
)

$ErrorActionPreference = 'Stop'

. "$PSScriptRoot\..\Shared\ScanScopeHelper.ps1"
. "$PSScriptRoot\..\Hooks\ClaudeTemp.ps1"

if (-not $OutputPath) { $OutputPath = New-ClaudeTempPath 'code-style-catalog.csv' }

if ($RepoPath) {
    $RepoPath = (Get-Item -LiteralPath $RepoPath).FullName
    if (-not $Repo) { $Repo = Split-Path -Leaf $RepoPath }
    $WorkspaceRoot = Split-Path -Parent $RepoPath
}
if (-not (Test-Path -LiteralPath $WorkspaceRoot -PathType Container)) {
    throw "WorkspaceRoot '$WorkspaceRoot' does not exist. Pass -WorkspaceRoot explicitly if the script was moved."
}
$WorkspaceRoot = (Get-Item -LiteralPath $WorkspaceRoot).FullName

$excludeDirNames = @(
    'bin', 'obj', 'node_modules', 'dist', 'dist-test', '.angular', 'venv', '.venv', '__pycache__',
    '.git', '.vs', '.idea', 'Migrations', 'coverage', '.astro', 'TestResults',
    'playwright-report', 'cucumber-report', '.features-gen', 'test-results', 'StrykerOutput'
)

# Repo-relative fragments excluded wholesale: generated mirrors, and this script's own directory so its
# own example patterns never appear as findings.
$excludePathFragments = @(
    '\Curator\build\lib\',
    '\Tools\Claude\'
)

$excludeFileSuffixes = @('.g.cs', '.Designer.cs', '.generated.cs', 'AssemblyInfo.cs', 'GlobalUsings.cs')

# Values production code uses at runtime - protocol/contract identifiers the SUT owns. A test hardcoding
# its own copy is rule 11's move-to-sut-constant case, not its generate case.
#
# Deliberately excludes the OIDC claim types that are also ordinary English words - `name`, `email`,
# `profile`, `role`. Including them mislabelled `Assert.Equal("name", ex.ParamName)` and
# `Columns.Add("Name", ...)` as protocol identifiers. The distinctive claim types below carry the signal
# without the collision; a bare `"name"` needs a human to say which sense it is, so it stays 'generate'.
$prodIdentifierPattern = '^(sub|iss|aud|exp|iat|jti|nbf|scope|scp|azp|amr|acr|Bearer|Basic|Negotiate|' +
    'Authorization|Content-Type|Accept|User-Agent|WWW-Authenticate|X-[\w-]+|application/json|text/plain|' +
    'text/event-stream|application/x-www-form-urlencoded|' +
    'client_id|client_secret|grant_type|refresh_token|access_token|authorization_code|code_verifier|' +
    'openid|offline_access|given_name|family_name)$'

# Filenames whose literals MAY be known-answer vectors or wire-format constants. The filename alone is
# not enough - CanonicalizationServiceTests matched an earlier, wider version of this pattern and its
# ordinary title-normalisation InlineData rows were all mislabelled keep-kat. So a filename
# hit only downgrades to needs-triage; the keep-kat disposition additionally requires the literal itself
# to look like a vector (long hex or base64).
$katFilePattern = '(TokenCrypto|Fingerprint|Crypto|Signature|Digest)'
$vectorLiteralPattern = '^([0-9a-fA-F]{16,}|[A-Za-z0-9+/]{24,}={0,2})$'

# xUnit/framework plumbing, not test data. `[Trait("Category","Unit")]`, `[Fact(DisplayName=...)]` and
# friends are the harness describing the test, so they are out of rule 11's scope entirely.
$frameworkAttributePattern = '^\s*\[\s*(Trait|Fact|Theory|Collection|CollectionDefinition|MemberData|ClassData|TestCaseOrderer|Obsolete|SuppressMessage|assembly:|(?:Given|When|Then|StepDefinition|StepArgumentTransformation)\s*\()'

$rows = New-Object System.Collections.Generic.List[object]

function Test-Excluded([System.IO.FileInfo]$file) {
    $parts = $file.FullName.Split([System.IO.Path]::DirectorySeparatorChar)
    foreach ($p in $parts) {
        if ($excludeDirNames -contains $p) { return $true }
        if ($p -match '^\.?venv') { return $true }
    }
    foreach ($frag in $excludePathFragments) {
        if ($file.FullName -like "*$frag*") { return $true }
    }
    foreach ($suffix in $excludeFileSuffixes) {
        if ($file.Name.EndsWith($suffix)) { return $true }
    }
    return $false
}

# Rule 2's Url/Uri arm mirrors CA1054/CA1056, so it honours the same .editorconfig scope the analyzer does:
# a section matching the file that sets `dotnet_diagnostic.CA1056.severity = none` (Identity silences it
# for `[Identity/Pages/**.cs]`, model-bound relative return URLs, with the reason recorded beside it). The
# .editorconfig files are found the way the compiler finds them: walking up from the file's directory
# until one says `root = true`, farther files applying first, so a repo-scoped run (-WorkspaceRoot
# pointing at one repo) reads the same files a workspace run does.
$script:EditorConfigCache = @{}

function ConvertTo-EditorConfigRegex([string]$glob) {
    $g = $glob
    if ($g.StartsWith('/')) { $g = $g.Substring(1) }
    elseif (-not $g.Contains('/')) { $g = '**/' + $g }
    $rx = [System.Text.StringBuilder]::new('^')
    $i = 0
    while ($i -lt $g.Length) {
        $c = [string]$g[$i]
        switch ($c) {
            '*' {
                if ($i + 1 -lt $g.Length -and $g[$i + 1] -eq '*') { [void]$rx.Append('.*'); $i++ }
                else { [void]$rx.Append('[^/]*') }
            }
            '?' { [void]$rx.Append('[^/]') }
            '{' { [void]$rx.Append('(?:') }
            '}' { [void]$rx.Append(')') }
            ',' { [void]$rx.Append('|') }
            '[' { [void]$rx.Append('[') }
            ']' { [void]$rx.Append(']') }
            default { [void]$rx.Append([regex]::Escape($c)) }
        }
        $i++
    }
    [void]$rx.Append('$')
    return $rx.ToString()
}

function Get-EditorConfigSections([string]$configDir) {
    if ($script:EditorConfigCache.ContainsKey($configDir)) { return $script:EditorConfigCache[$configDir] }
    $sections = New-Object System.Collections.Generic.List[object]
    $path = Join-Path $configDir '.editorconfig'
    if (Test-Path -LiteralPath $path) {
        $current = $null
        foreach ($line in (Get-Content -LiteralPath $path)) {
            $trim = $line.Trim()
            if ($trim -eq '' -or $trim.StartsWith('#') -or $trim.StartsWith(';')) { continue }
            if ($trim -match '^\[(.+)\]$') {
                $current = @{ Regex = ConvertTo-EditorConfigRegex $Matches[1]; Settings = @{} }
                $sections.Add($current)
                continue
            }
            if ($null -ne $current -and $trim -match '^([^=]+?)\s*=\s*([^#;]*)') {
                $current.Settings[$Matches[1].Trim().ToLowerInvariant()] = $Matches[2].Trim().ToLowerInvariant()
            }
        }
    }
    $script:EditorConfigCache[$configDir] = $sections
    return $sections
}

function Test-DiagnosticSilenced([string]$repo, [string]$relPath, [string]$diagnosticId) {
    $fullPath = Join-Path $WorkspaceRoot $relPath
    $key = "dotnet_diagnostic.$($diagnosticId.ToLowerInvariant()).severity"
    $configDirs = New-Object System.Collections.Generic.List[string]
    $dir = Split-Path -Parent $fullPath
    while (-not [string]::IsNullOrEmpty($dir)) {
        $candidate = Join-Path $dir '.editorconfig'
        if (Test-Path -LiteralPath $candidate) {
            $configDirs.Add($dir)
            if ((Get-Content -LiteralPath $candidate -Raw) -match '(?im)^\s*root\s*=\s*true\s*$') { break }
        }
        $parent = Split-Path -Parent $dir
        if ($parent -eq $dir) { break }
        $dir = $parent
    }
    $silenced = $false
    for ($i = $configDirs.Count - 1; $i -ge 0; $i--) {
        $configDir = $configDirs[$i]
        $relToConfig = $fullPath.Substring($configDir.Length).TrimStart('\').Replace('\', '/')
        foreach ($section in (Get-EditorConfigSections $configDir)) {
            if ($relToConfig -match $section.Regex -and $section.Settings.ContainsKey($key)) {
                $silenced = ($section.Settings[$key] -eq 'none')
            }
        }
    }
    return $silenced
}

# Rule 2's inverse arm (a `Uri` auto-property) exists for one failure: OData's EDM turns a `Uri` into a
# complex type. An entity POCO never names OData itself, so the arm is judged per REPO by a csproj that
# references an OData package; a repo without one has no EDM to declare.
#
# The repo root is the nearest ancestor holding a `.git`, NOT `$WorkspaceRoot\<first path segment>`: a
# run scoped to one repo (`-WorkspaceRoot ...\Functions`) makes that segment the PROJECT folder, so a
# `Uri` property in a test project would be judged by the test csproj alone and silently dropped. A
# false negative is the failure mode to avoid here. With no `.git` (the harness fixtures) the first
# segment is the fallback, which is what keeps the two fixture repos judged apart.
$script:ODataRepoCache = @{}

function Get-RepoRootForFile([string]$repo, [string]$relPath) {
    $dir = Split-Path -Parent (Join-Path $WorkspaceRoot $relPath)
    while ($dir.StartsWith($WorkspaceRoot, [StringComparison]::OrdinalIgnoreCase)) {
        if (Test-Path -LiteralPath (Join-Path $dir '.git')) { return $dir }
        $parent = Split-Path -Parent $dir
        if ($parent -eq $dir) { break }
        $dir = $parent
    }
    return (Join-Path $WorkspaceRoot $repo)
}

function Test-RepoUsesOData([string]$repo, [string]$relPath) {
    $repoRoot = Get-RepoRootForFile $repo $relPath
    if (-not $script:ODataRepoCache.ContainsKey($repoRoot)) {
        $uses = $false
        foreach ($proj in (Get-ChildItem -LiteralPath $repoRoot -Recurse -Filter *.csproj -ErrorAction SilentlyContinue)) {
            if (Test-Excluded $proj) { continue }
            if ((Get-Content -LiteralPath $proj.FullName -Raw) -match 'OData') { $uses = $true; break }
        }
        $script:ODataRepoCache[$repoRoot] = $uses
    }
    return $script:ODataRepoCache[$repoRoot]
}

# Where the OData arm above stops. `modelBuilder.Ignore<T>()` removes T from the EDM outright, so T is
# never an entity or a complex type and its `Uri` property cannot 400 a PATCH - the one failure the arm
# exists to prevent. Judged from the model-builder call in the repo, which is the only place that fact is
# written; the property itself cannot say it. The declaring type is taken from the file name (one type per
# file, fleet-wide), so a mismatch leaves the row standing rather than silencing it.
$script:EdmIgnoredTypeCache = @{}

function Test-TypeIgnoredFromEdm([string]$repo, [string]$relPath, [string]$typeName) {
    $repoRoot = Get-RepoRootForFile $repo $relPath
    if (-not $script:EdmIgnoredTypeCache.ContainsKey($repoRoot)) {
        $ignored = New-Object 'System.Collections.Generic.HashSet[string]' ([StringComparer]::Ordinal)
        foreach ($file in (Get-ChildItem -LiteralPath $repoRoot -Recurse -Filter *.cs -File -ErrorAction SilentlyContinue)) {
            if (Test-Excluded $file) { continue }
            $text = Get-Content -LiteralPath $file.FullName -Raw -ErrorAction SilentlyContinue
            if (-not $text) { continue }
            foreach ($m in [regex]::Matches($text, '\.Ignore<\s*(\w+)\s*>\s*\(')) {
                [void]$ignored.Add($m.Groups[1].Value)
            }
        }
        $script:EdmIgnoredTypeCache[$repoRoot] = $ignored
    }
    return $script:EdmIgnoredTypeCache[$repoRoot].Contains($typeName)
}

# Rule 2 on a string property is decided by EVIDENCE that a stronger type exists, never by the property's
# name. The evidence is the repo's own production code converting that member with a conversion that
# THROWS: `Guid.Parse(x.RunId)`, `new Uri(x.Website)`, `Enum.Parse<Kind>(x.Kind)`. A throwing conversion
# asserts the value is always valid, which is a stronger type the declaration refuses to state. `TryParse`
# is the opposite claim - the text may not be valid - so it is a boundary reading someone else's text (a
# CSV field, a header, a vendor payload) and does not count. An identifier minted by someone else (a PSN
# title id, a Duende client id, a WebAuthn credential id) is a string, and nothing in the code turns it
# into anything else, so it never reports.
#
# The arm this replaced flagged every string property whose name ended in Id, Date, Time, Count, Kind,
# Status, Type or State. A name is not evidence: it charged 57 opaque vendor and framework identifiers as
# rule-2 candidates and would have pushed wrapper types onto all of them.
#
# Keyed by member NAME per repo, because a text scan cannot see which type a receiver has. Two types that
# share a member name therefore share the evidence; the harness pins a same-named property in another repo
# staying silent. Test projects are not read: a test converting a value says nothing about production.
$script:ParsedStringMemberCache = @{}

function Get-ParsedStringMembers([string]$repo, [string]$relPath) {
    $repoRoot = Get-RepoRootForFile $repo $relPath
    if (-not $script:ParsedStringMemberCache.ContainsKey($repoRoot)) {
        $parsed = New-Object 'System.Collections.Generic.Dictionary[string,string]' ([StringComparer]::Ordinal)
        $member = '(?:[\w\]\[)(]+[?!]?\.)+(?<name>[A-Z]\w*)\b(?!\s*[(<])'
        $conversions = @(
            "\b(?<type>Guid|DateTimeOffset|DateTime|DateOnly|TimeOnly|TimeSpan|int|long|short|decimal|double|float|bool|Int32|Int64|Decimal|Double|Boolean)\.Parse(?:Exact)?\s*\(\s*$member"
            "\bnew\s+(?<type>Uri)\s*\(\s*$member"
            "\bEnum\.Parse\s*<\s*(?<type>\w+)\s*>\s*\(\s*$member"
            "\bConvert\.To(?<type>Int32|Int64|Decimal|Double|DateTime|Boolean)\s*\(\s*$member"
        )
        foreach ($file in (Get-ChildItem -LiteralPath $repoRoot -Recurse -Filter *.cs -File -ErrorAction SilentlyContinue)) {
            if (Test-Excluded $file) { continue }
            $rel = $file.FullName.Substring($WorkspaceRoot.Length).TrimStart('\')
            if ((Get-Area $rel) -ne 'prod') { continue }
            $text = Get-Content -LiteralPath $file.FullName -Raw -ErrorAction SilentlyContinue
            if (-not $text) { continue }
            $code = [regex]::Replace($text, '(?m)^\s*//.*$', '')
            foreach ($pattern in $conversions) {
                foreach ($m in [regex]::Matches($code, $pattern)) {
                    $name = $m.Groups['name'].Value
                    if (-not $parsed.ContainsKey($name)) { $parsed[$name] = $m.Groups['type'].Value }
                }
            }
        }
        $script:ParsedStringMemberCache[$repoRoot] = $parsed
    }
    return $script:ParsedStringMemberCache[$repoRoot]
}

function Get-RepoName([string]$fullPath, [string]$root) {
    if ($RepoPath -and $fullPath.StartsWith($RepoPath + '\', [StringComparison]::OrdinalIgnoreCase)) { return $Repo }
    $rel = $fullPath.Substring($root.Length).TrimStart('\')
    return $rel.Split('\')[0]
}

# The area boundary moved to Tools/Shared/ScanScopeHelper.ps1 when Get-CommentCatalog.ps1 needed it too:
# COMMENTS.md's bare-marker allowance is now scoped to unit tests, which is the same boundary rule 18 uses,
# and two copies could disagree about one path and make a marker allowed and forbidden at once.
function Get-Area([string]$relPath) {
    return Get-ScanArea -RelativePath $relPath
}

function Add-Row($repo, $relPath, $lineNum, $area, $ruleNum, $category, $disposition, $snippet) {
    $rows.Add([pscustomobject]@{
        Repo        = $repo
        Path        = $relPath
        Line        = $lineNum
        Area        = $area
        Rule        = $ruleNum
        Category    = $category
        Disposition = $disposition
        Snippet     = ($snippet.Trim() -replace '\s+', ' ')
    })
}

# The literals on a line, as spans, for rule 11's sweep. A regex pairing quotes left to right cannot read
# C#: a nested interpolated string (`$"{Wrap(x, $" {y} ")}iota beside"`) pairs the OUTER opening quote with
# the INNER one, so the scan reports fragments that were never literals and buries the line's real text
# inside one of them. Filtering those fragments by unbalanced braces silences the real text with them,
# which is what the harness control 'still flags an ordinary literal beside a nested interpolation' caught.
# So walk the line instead: track interpolation holes, and consume a literal that opens inside a hole whole,
# so its closing quote cannot be mistaken for the outer literal's.
function Get-CodeLiterals([string]$code) {
    $literals = New-Object System.Collections.Generic.List[object]
    $i = 0
    while ($i -lt $code.Length) {
        if ($code[$i] -ne '"') { $i++; continue }

        $start = $i
        $isVerbatim = ($start -gt 0 -and $code[$start - 1] -eq '@') -or
            ($start -gt 1 -and $code[$start - 1] -eq '$' -and $code[$start - 2] -eq '@')
        $hole = 0
        $j = $start + 1
        $closed = $false
        while ($j -lt $code.Length) {
            $c = $code[$j]
            if (-not $isVerbatim -and $c -eq '\') { $j += 2; continue }
            if ($c -eq '{') { $hole++; $j++; continue }
            if ($c -eq '}') { if ($hole -gt 0) { $hole-- }; $j++; continue }
            if ($c -ne '"') { $j++; continue }
            if ($isVerbatim -and $j + 1 -lt $code.Length -and $code[$j + 1] -eq '"') { $j += 2; continue }
            if ($hole -gt 0) {
                $k = $j + 1
                while ($k -lt $code.Length -and $code[$k] -ne '"') {
                    if ($code[$k] -eq '\') { $k++ }
                    $k++
                }
                $j = $k + 1
                continue
            }

            $closed = $true
            break
        }

        if (-not $closed) { break }
        $literals.Add([pscustomobject]@{
            Index = $start
            Length = $j - $start + 1
            Value = $code.Substring($start, $j - $start + 1)
        })
        $i = $j + 1
    }

    return , $literals
}

# Blanks out string/char literal bodies so a rule word inside a literal can't fire. Keeps the quotes so
# patterns that legitimately target "a literal is present here" (rules 1, 8) still see them.
function Remove-LiteralBodies([string]$line) {
    $out = [regex]::Replace($line, '@"(?:[^"]|"")*"', '@""')
    $out = [regex]::Replace($out, '"(?:\\.|[^"\\])*"', '""')
    $out = [regex]::Replace($out, "'(?:\\.|[^'\\])'", "''")
    return $out
}

# `//` inside a string literal is NOT a comment. The naive `'//.*$'` truncated `url.StartsWith("http://",
# StringComparison.OrdinalIgnoreCase)` at the `//` inside the literal, which both hid the rest of the line
# from every rule and made a compliant call look like a bare one. Walk the line tracking literal state and
# cut only at a `//` that is actually outside one.
# True when THIS call's own argument list states a comparison. Scoping the rule-8 exemption to the call
# rather than the whole line matters more as the fleet complies, not less: with a line-level test,
# `path.StartsWith(prefix) && string.Equals(a, b, StringComparison.Ordinal)` hides the bare StartsWith
# behind the compliant Equals, so the detector would go blind exactly where adoption was working.
#
# Comparer<T> states the comparison for any T except string: Comparer<string>.Default is the very
# culture-sensitive default the OrderBy rows exist to catch, so naming it states the defect.
function Test-CallStatesComparison([string]$code, [int]$nameIndex) {
    $comparisonWords = 'StringComparison|StringComparer|EqualityComparer<|(?<!\w)Comparer<(?!\s*(?:System\.)?[Ss]tring\s*>)|CultureInfo|IFormatProvider'
    $open = $code.IndexOf('(', $nameIndex)
    if ($open -lt 0) { return $false }
    $depth = 0
    for ($i = $open; $i -lt $code.Length; $i++) {
        if ($code[$i] -eq '(') { $depth++ }
        elseif ($code[$i] -eq ')') {
            $depth--
            if ($depth -eq 0) {
                return $code.Substring($open, $i - $open + 1) -match $comparisonWords
            }
        }
    }
    # Unbalanced - the call is continued on the next line. Judge on what is visible rather than guessing.
    return $code.Substring($open) -match $comparisonWords
}

# Rule 1 lets an empty string through as a call argument only where it is a SEPARATOR: the text an API
# inserts or removes (`value.Replace(" ", string.Empty)`, `string.Join(string.Empty, parts)`). Passed to
# anything else it is a value, and the value is "known to be blank" standing in for absence:
# `EnrichmentNeed.EveryProvider(string.Empty)` handed a fake game id, and `Roles.Add(string.Empty)` bound a
# blank role. The callee decides, so find the call that owns the argument and check its name against the
# separator APIs. Production only: a test passing an empty string is feeding the code under test the
# boundary input rule 11 keeps.
$emptySeparatorCallees = '^(?:Replace|Join|Split|Concat|Trim|TrimStart|TrimEnd|Insert|Append|AppendJoin)$'

# `ModelState.AddModelError("", message)` is the framework's own spelling of a model-level error: the key
# names the property the error belongs to, and the empty key means "the model itself". `null` is not the
# same value there, it throws ArgumentNullException, so this empty string is the API's mandated key rather
# than a value standing in for absence. Anchored on the callee AND the first argument position, so an empty
# string in the message position keeps reporting.
$frameworkEmptyKeyCallees = '^AddModelError$'

function Test-EmptyStringValueArgument([string]$code, [string]$bareEmptyPattern) {
    # Non-empty literals become "x" so their contents cannot fake a parenthesis or an empty string.
    $shaped = [regex]::Replace($code, '"(?:\\.|[^"\\])+"', '"x"')
    foreach ($m in [regex]::Matches($shaped, "(?<=[(,]\s*)(?:string\.Empty|`"`"|$bareEmptyPattern)(?=\s*[,)])")) {
        $depth = 0
        for ($i = $m.Index - 1; $i -ge 0; $i--) {
            $c = $shaped[$i]
            if ($c -eq ')') { $depth++ }
            elseif ($c -eq '(') {
                if ($depth -gt 0) { $depth--; continue }
                $before = $shaped.Substring(0, $i)
                if ($before -match '(\w+)\s*(?:<[^<>]*>)?\s*$') {
                    $callee = $Matches[1]
                    $isFirstArgument = $shaped.Substring($i + 1, $m.Index - $i - 1).Trim() -eq ''
                    if ($callee -match $frameworkEmptyKeyCallees -and $isFirstArgument) { break }
                    if ($callee -ne 'nameof' -and $callee -notmatch $emptySeparatorCallees) { return $true }
                }
                break
            }
        }
    }
    return $false
}

# Rule 8 over an EF Core IQueryable is not a violation and cannot be made one: the expression becomes SQL
# sorted and compared by the DATABASE collation, and `string.Equals(..., StringComparison)` has no SQL
# translation - EF throws at execution while the build stays green. So a directive here would turn working
# code into a runtime bug, which is why the OrderBy family was emitted as needs-triage in the first place.
# needs-triage was the right call while this was undecidable; it is decidable from the receiver, so the
# judgement belongs here rather than in a prose list that every run has to be re-checked against.
#
# Deliberately narrow, so this teaches the detector rather than weakening it: an EF async terminal operator
# (which only exists on IQueryable), a DbContext-shaped receiver, or ASP.NET Identity's own queryable
# collections. LINQ over an in-memory list still reports, because that is where the culture default bites.
# Rule 10's positional operator, which is rule 10's subject only when it narrows a LOCATOR. Two
# independent line-level matches - one for `Locator(`, one for `.First` - read
# `Expect(page.Locator("#products-table")).Not.ToContainTextAsync(names.First)` as a positional selector,
# when `names` is a generated-name pair and `#products-table` is exactly the id the rule asks for. This
# walks the locator call to its matching paren and then follows only the member chain hanging off it, so
# an argument, a sibling call and a LINQ result on another receiver are all out of reach.
#
# Language-scoped because C#'s .First/.Last are properties - the lookahead is what keeps LINQ's .First()
# out - while TypeScript's are methods; the C# form applied to .ts hid `page.locator(NAV_LABELS).first()`.
# Literal bodies are blanked first, so a parenthesis inside a selector cannot move the depth.
function Test-PositionalSelector([string]$code, [string]$ext) {
    $blanked = [regex]::Replace($code, '"(?:\\.|[^"\\])*"', '""')
    $blanked = [regex]::Replace($blanked, "'(?:\\.|[^'\\])*'", "''")
    $positional = if ($ext -eq '.cs') { '^\.(?:First|Last)\b(?!\s*\()|^\.Nth\s*\(' }
                  else { '^\.(?:first|last)\s*\(\s*\)|^\.nth\s*\(' }

    $skipCall = {
        param($text, $start)
        $depth = 1
        $i = $start
        while ($i -lt $text.Length -and $depth -gt 0) {
            if ($text[$i] -eq '(') { $depth++ }
            elseif ($text[$i] -eq ')') { $depth-- }
            $i++
        }
        return $i
    }

    foreach ($m in [regex]::Matches($blanked, '(?i)\b(?:locator|getby[a-z]+)\s*\(')) {
        $i = & $skipCall $blanked ($m.Index + $m.Length)
        while ($i -lt $blanked.Length -and $blanked[$i] -eq '.') {
            $rest = $blanked.Substring($i)
            if ($rest -match $positional) { return $true }
            if ($rest -notmatch '^\.(\w+)') { break }
            $i += 1 + $Matches[1].Length
            if ($i -lt $blanked.Length -and $blanked[$i] -eq '(') { $i = & $skipCall $blanked ($i + 1) }
        }
    }
    return $false
}

function Test-EfQueryable([string]$code) {
    $efAsyncTerminals = '\.(?:First|FirstOrDefault|Single|SingleOrDefault|Any|All|Count|LongCount|ToList|' +
                        'ToArray|ToDictionary|Sum|Max|Min|Average|ExecuteUpdate|ExecuteDelete|Load)Async\s*\('
    if ($code -match $efAsyncTerminals) { return $true }
    if ($code -match '(?<![\w.])_?(?:db|context)\w*\.') { return $true }
    if ($code -match '(?<![\w.])_?\w*(?:User|Role)Manager\.(?:Users|Roles)\b') { return $true }
    return $false
}

function Remove-LineComment([string]$line) {
    $inString = $false
    $inChar = $false
    $inTemplate = $false
    $verbatim = $false
    for ($i = 0; $i -lt $line.Length; $i++) {
        $c = $line[$i]
        if ($inTemplate) {
            if ($c -eq '\') { $i++ }
            elseif ($c -eq '`') { $inTemplate = $false }
        }
        elseif ($inString) {
            if ($verbatim) {
                if ($c -eq '"') {
                    if ($i + 1 -lt $line.Length -and $line[$i + 1] -eq '"') { $i++ }
                    else { $inString = $false; $verbatim = $false }
                }
            }
            elseif ($c -eq '\') { $i++ }
            elseif ($c -eq '"') { $inString = $false }
        }
        elseif ($inChar) {
            if ($c -eq '\') { $i++ }
            elseif ($c -eq "'") { $inChar = $false }
        }
        elseif ($c -eq '"') {
            $inString = $true
            $verbatim = ($i -gt 0 -and $line[$i - 1] -eq '@')
        }
        elseif ($c -eq "'") { $inChar = $true }
        elseif ($c -eq '`') { $inTemplate = $true }
        elseif ($c -eq '/' -and $i + 1 -lt $line.Length -and $line[$i + 1] -eq '/') {
            return $line.Substring(0, $i)
        }
    }
    return $line
}

# Rule 13's one honest exemption, and it is a SHAPE rather than a base-type allow-list. A base constructor
# can demand an ILogger; passing one through to satisfy it is not the code narrating, because the class has
# no logger to call. The judgement the rule actually turns on is whether OUR class keeps the parameter: the
# moment it is assigned to an instance member, the class holds a logger and rule 13 applies again.
#
# This replaced a file-level `: AuthenticationHandler<` flag, which was wrong twice over. Too narrow: it
# named ONE base class, so every other base with the same constructor demand still reported. Too broad: the
# flag applied to every ILoggerFactory line in the file, so a handler that assigned the parameter to a field
# and logged from it was exempted along with the pass-through. It also emitted a 'keep-framework-mandated'
# row, which is a standing-exemption list rebuilt inside the catalog: a row a reader can only ever dismiss.
#
# Both checks fail toward REPORTING. An unparsed constructor yields no names, and any `= <name>` anywhere in
# the file (a local included) disqualifies the name, so the arm can only ever lose an exemption, never gain
# a silence.
function Get-BasePassThroughLoggerParameters([string]$text) {
    $names = New-Object 'System.Collections.Generic.HashSet[string]' ([StringComparer]::Ordinal)
    # Two spellings of the same thing, and the fleet uses both: `: base(...)` on an ordinary constructor,
    # and `: SomeBase<T>(...)` on a primary constructor, which names the base type instead of `base`.
    # Missing the second left every primary-constructor handler reporting.
    #
    # The lookbehind anchors the `:` to a line start or to the closing paren of the parameter list, which
    # is where a base initializer sits and where a ternary's `:` does not. `: this(...)` is deliberately
    # NOT matched: that hands the parameter to another constructor of the same class, which may keep it.
    $initializerPattern = '(?m)(?<=(?:^|\))\s*):\s*(?:base|[A-Z]\w*(?:\s*<[^>]*>)?)\s*\(([^()]*)\)'
    # Almost no file has a base initializer at all, and this runs on every .cs file in the workspace.
    if ($text -notmatch $initializerPattern) { return $names }
    foreach ($initializer in [regex]::Matches($text, $initializerPattern)) {
        $baseArguments = $initializer.Groups[1].Value
        # The parameter list is the parenthesised group immediately before the initializer. Walked
        # backwards from its closing paren rather than matched by a nested regex: a `(...)` pattern that
        # tolerates one nesting level backtracks catastrophically on a large file when the match fails,
        # and this runs over the whole workspace.
        $head = $text.Substring(0, $initializer.Index)
        $close = $head.LastIndexOf(')')
        if ($close -lt 0) { continue }
        $depth = 0
        $open = -1
        for ($i = $close; $i -ge 0; $i--) {
            if ($head[$i] -eq ')') { $depth++ }
            elseif ($head[$i] -eq '(') {
                $depth--
                if ($depth -eq 0) { $open = $i; break }
            }
        }
        if ($open -lt 0) { continue }
        $parameters = $head.Substring($open + 1, $close - $open - 1)
        foreach ($p in [regex]::Matches($parameters, '\bILogger(?:Factory)?(?:\s*<[^>]*>)?\s+(\w+)')) {
            $name = $p.Groups[1].Value
            $bounded = [regex]::Escape($name)
            # It has to actually reach the base constructor. A logger parameter the base never receives is
            # not mandated by anything.
            if ($baseArguments -notmatch "(?<![\w.])$bounded(?![\w])") { continue }
            # And our class must never keep it. `(?<![=!<>])=` skips ==/!=/<=/>=; `=>` cannot match because
            # `>` is not whitespace. `_loggerFactory = logger` and `_logger = logger.CreateLogger<T>()` both
            # disqualify, which is the case rule 13 exists for.
            if ($text -match "(?<![=!<>])=\s*$bounded(?![\w])") { continue }
            [void]$names.Add($name)
        }
    }
    return $names
}

# Shared by Scan-CSharpLine and Scan-TypeScriptLine deliberately: `!` is the same null-forgiving escape
# hatch in both languages, and rule 9 says "anywhere". One definition so the two cannot drift apart.
$script:NullForgivingPattern = '[\w)\]]!(?!=)\s*[.,;)\]]'

function Scan-CSharpLine($repo, $relPath, $lineNum, $area, $raw, $continuation = '', $mandatedLoggerParameters = $null, $statementPrefix = '') {
    if ($null -eq $mandatedLoggerParameters) {
        $mandatedLoggerParameters = New-Object 'System.Collections.Generic.HashSet[string]' ([StringComparer]::Ordinal)
    }
    $code = Remove-LineComment $raw
    if ($code.Trim() -eq '') { return }
    $noStr = Remove-LiteralBodies $code
    # An argument list that spans lines is still one call, and reading it to its closing paren is
    # deterministic rather than a guess. Without this, every Allman-wrapped
    # `string.Equals(\n a,\n b,\n StringComparison.Ordinal)` reported as `state-ordinal` against code that
    # already states the comparison - a false positive that reproduces on every run and that a reader has
    # to re-dismiss by hand each time. Indices from $noStr stay valid because the continuation is appended
    # after it.
    $noStrWithContinuation = if ($continuation -eq '') { $noStr } else { $noStr + "`n" + $continuation }

    # `global using static System.String` makes a bare `Empty` mean `string.Empty`, and this detector
    # matched only the qualified spelling - so `public string ClientId { get; set; } = Empty;` in
    # Identity's Pages/Account/Manage/Grants.cshtml.cs was a rule-1 violation nothing reported, under a
    # global using declared in a DIFFERENT file (Identity/Properties/GlobalUsings.cs:16). Rule 5 already
    # reads the same using-static form for bare `IsNullOrEmpty(`; rule 1 not doing so was the
    # inconsistency, not a deliberate narrowing.
    #
    # The lookbehind is what keeps this off everything else called Empty: a `.` before it means the
    # member belongs to someone else (`Array.Empty<string>()`, `ImmutableArray<int>.Empty`), and the
    # lookahead rejects a generic or invocation (`Empty<T>()`) and a longer identifier (`EmptyLabel`).
    # All three are pinned as must-not-fire controls beside the three must-fire ones.
    $bareEmpty = '(?<![.\w])Empty(?![\w<(])'
    if ($Rule -contains 1) {
        if ($code -match "\?\?\s*(string\.Empty|`"`"|$bareEmpty)") {
            Add-Row $repo $relPath $lineNum $area 1 'null-coalesce-to-empty' 'use-null' $raw
        }
        elseif ($code -match "\breturn\s+(string\.Empty|`"`"|$bareEmpty)\s*;") {
            Add-Row $repo $relPath $lineNum $area 1 'returns-empty-string' 'use-null' $raw
        }
        elseif ($code -match "=>\s*(string\.Empty|`"`"|$bareEmpty)\s*[;,)]") {
            Add-Row $repo $relPath $lineNum $area 1 'returns-empty-string' 'use-null' $raw
        }
        elseif ($code -match "=\s*(string\.Empty|`"`"|$bareEmpty)\s*[;,)}]") {
            Add-Row $repo $relPath $lineNum $area 1 'assigned-empty-string' 'use-null' $raw
        }
        elseif ($area -eq 'prod' -and (Test-EmptyStringValueArgument $code $bareEmpty)) {
            Add-Row $repo $relPath $lineNum $area 1 'empty-string-argument' 'use-null' $raw
        }
    }

    if ($Rule -contains 2) {
        # The Url/Uri arm mirrors CA1056, Microsoft's own rule, so it keeps the suffix and the analyzer's
        # .editorconfig scope. Every other string property reports only on conversion evidence; see
        # Get-ParsedStringMembers.
        if ($noStr -match '\b(public|internal|protected)\s+(required\s+)?string\??\s+(\w*(?:Url|Uri))\b\s*[{=]') {
            $propertyName = $Matches[3]
            if (-not (Test-DiagnosticSilenced $repo $relPath 'CA1056')) {
                Add-Row $repo $relPath $lineNum $area 2 "string-property-named-$propertyName" 'use-uri' $raw
            }
        }
        elseif ($noStr -match '\b(public|internal|protected)\s+(required\s+)?string\??\s+(\w+)\b\s*[{=]') {
            $propertyName = $Matches[3]
            $parsedMembers = Get-ParsedStringMembers $repo $relPath
            if ($parsedMembers.ContainsKey($propertyName)) {
                $parsedAs = $parsedMembers[$propertyName]
                Add-Row $repo $relPath $lineNum $area 2 "string-property-parsed-as-$parsedAs" "use-$($parsedAs.ToLowerInvariant())" $raw
            }
        }
        elseif ($noStr -match '\b(public|internal|protected)\s+(required\s+)?Uri\??\s+\w+\s*\{\s*get' -and (Test-RepoUsesOData $repo $relPath)) {
            $declaringType = [System.IO.Path]::GetFileNameWithoutExtension($relPath)
            if (-not (Test-TypeIgnoredFromEdm $repo $relPath $declaringType)) {
                Add-Row $repo $relPath $lineNum $area 2 'uri-property-check-the-declared-schema' 'needs-triage' $raw
            }
        }
        elseif ($area -eq 'prod' -and $script:ParsedStringLocals) {
            foreach ($m in [regex]::Matches($noStr, '\bstring\??\s+([a-z_]\w*)\s*(?=[,);]|=(?!>))')) {
                $localName = $m.Groups[1].Value
                if ($script:ParsedStringLocals.ContainsKey($localName)) {
                    $parsedAs = $script:ParsedStringLocals[$localName]
                    Add-Row $repo $relPath $lineNum $area 2 "string-parameter-parsed-as-$parsedAs" "use-$($parsedAs.ToLowerInvariant())" $raw
                    break
                }
            }
        }
    }

    if ($Rule -contains 3) {
        if ($noStr -match '\bDateTime\.(Now|UtcNow|Today)\b') {
            Add-Row $repo $relPath $lineNum $area 3 'datetime-now' 'use-datetimeoffset' $raw
        }
        elseif ($noStr -match '\bDateTimeKind\b') {
            Add-Row $repo $relPath $lineNum $area 3 'datetimekind' 'use-timespan-zero' $raw
        }
        elseif ($noStr -match '\bGetDateTime\s*\(') {
            Add-Row $repo $relPath $lineNum $area 3 'reader-getdatetime' 'use-getfieldvalue-dto' $raw
        }
        elseif ($noStr -match '(?<!\w)DateTime\??\s+\w+\s*[{=;,)]' -and $noStr -notmatch 'DateTimeOffset') {
            Add-Row $repo $relPath $lineNum $area 3 'datetime-declaration' 'use-datetimeoffset' $raw
        }
    }

    if ($Rule -contains 4) {
        if ($noStr -match '\b(List|IList|ICollection|IEnumerable|IReadOnlyList|IReadOnlyCollection|HashSet|Dictionary|IDictionary|IReadOnlyDictionary|Collection)<[^>]*>\?\s+\w+') {
            Add-Row $repo $relPath $lineNum $area 4 'nullable-generic-collection' 'use-empty-collection' $raw
        }
        elseif ($noStr -match '\w\[\]\?\s+\w+') {
            Add-Row $repo $relPath $lineNum $area 4 'nullable-array' 'use-empty-collection' $raw
        }
    }

    # `cached.IsNullOrEmpty` (StackExchange.Redis RedisValue) must NOT match: it is a property, not a
    # string call. Requiring either the `string.` qualifier or a non-member-access call form excludes it.
    if ($Rule -contains 5) {
        if ($noStr -match '\bstring\.IsNullOrEmpty\s*\(' -or $noStr -match '(?<![.\w])IsNullOrEmpty\s*\(') {
            Add-Row $repo $relPath $lineNum $area 5 'isnullorempty' 'use-isnullorwhitespace' $raw
        }
    }

    # Rule 8 splits by WHY a call is wrong, because the two halves have different urgency. A method that is
    # culture-sensitive BY DEFAULT changes behaviour with the server's locale, so it is a latent bug; one
    # that is ordinal by default is merely unstated, and the fix is to say so. That is the same split
    # Microsoft draws between CA1310 (correctness) and CA1307 (clarity).
    # An EF Core IQueryable suppresses the whole rule, not one arm of it: `==`, StartsWith and the OrderBy
    # family all translate to SQL, and every rule 8 fix - a StringComparison argument or a StringComparer -
    # is untranslatable. Applying any of them turns a working query into a runtime throw that the build
    # still reports green.
    # The receiver of a wrapped query sits on an earlier physical line (`await _context.Keys` above
    # `.OrderByDescending(...)`), so the statement so far is read alongside this line.
    if (($Rule -contains 8) -and -not (Test-EfQueryable (($statementPrefix, $noStr) -join "`n"))) {
        # Read the BLANKED line: a real `kind == "x"` still leaves `== ""` behind, while `$"=== {name} ==="`
        # and a base64 literal ending in `==` leave nothing. Reading $code here scored both as comparisons.
        if ($noStr -match '[=!]=\s*"' -or $noStr -match '"\s*[=!]=' ) {
            Add-Row $repo $relPath $lineNum $area 8 'string-literal-equality' 'use-string-equals-ordinal' $raw
        }
        elseif ($noStr -match '[=!]=\s*string\.Empty' -or $noStr -match 'string\.Empty\s*[=!]=') {
            Add-Row $repo $relPath $lineNum $area 8 'string-empty-equality' 'use-isnullorwhitespace' $raw
        }
        else {
            # Ordered most-severe first, and at most one row per line, like every other rule here. The
            # `""` in the StartsWith/Contains/Equals patterns is a BLANKED STRING literal, which is what
            # keeps the char overloads out - `StartsWith('{')` and `IndexOf('?')` are ordinal already and
            # correct as written, and the only thing distinguishing them is the quote character.
            $ruleEightChecks = @(
                @{ Rx = '\.(StartsWith|EndsWith|IndexOf|LastIndexOf)\s*\(\s*""'; Cat = 'culture-default-{0}'; Disp = 'use-ordinal' }
                @{ Rx = '(?<![.\w])(string\.Compare)\s*\('; Cat = 'culture-default-compare'; Disp = 'use-ordinal' }
                # CompareTo has NO StringComparison overload, so it cannot be fixed in place - it has to
                # become string.Compare(a, b, StringComparison.Ordinal).
                @{ Rx = '\.(CompareTo)\s*\('; Cat = 'compareto-no-overload'; Disp = 'use-string-compare-ordinal' }
                # Case-folding to compare is the Turkish-I bug waiting to happen; OrdinalIgnoreCase replaces it.
                @{ Rx = '\.(To(?:Lower|Upper))\s*\(\s*\)'; Cat = 'case-fold-to-compare'; Disp = 'use-ordinalignorecase' }
                # Ordinal by default, but the contract is unstated.
                @{ Rx = '(?<![.\w])(string\.Equals)\s*\('; Cat = 'equals-without-comparison'; Disp = 'state-ordinal' }
                @{ Rx = '\.(Equals)\s*\(\s*""'; Cat = 'equals-without-comparison'; Disp = 'state-ordinal' }
                # Two receivers whose Contains is membership rather than string search stay out:
                # DbParameterCollection.Contains(string) has no StringComparison overload, and xUnit's
                # Assert.Contains(item, collection) is set membership (its string-in-string overload is
                # CA1307's, which sees the types this text scan cannot).
                @{ Rx = '(?<!\bAssert|[Pp]arameters)\.(Contains|Replace)\s*\(\s*""'; Cat = 'ordinal-default-{0}'; Disp = 'state-ordinal' }
                # Ordering over strings picks up Comparer<string>.Default, which is culture-sensitive; the
                # equality-based operators (GroupBy, ToDictionary, Except, Intersect, Union, Distinct) pick up
                # EqualityComparer<string>.Default, which is ordinal but unstated - but only for
                # LINQ-to-Objects. The identical expression over an EF
                # IQueryable becomes SQL sorted by the DATABASE collation, where a StringComparer is not
                # expressible, and the element may not be a string at all. needs-triage, never a directive.
                # A Distinct that already carries an argument has supplied its comparer; only the bare call
                # picks up Comparer<T>.Default.
                # `(?!\))` - an EMPTY argument list is never LINQ. Every one of these operators except
                # Distinct requires a key selector or a second sequence, so a no-argument call is a
                # fluent configuration builder that merely shares the name: `AddOData(o => o.Select()
                # .Filter().OrderBy())` enables the OData system query options, sorts nothing, and has no
                # comparer to state. Distinct keeps its own arm because for IT the empty list is the
                # reportable case - an argument is the comparer.
                @{ Rx = '\.(OrderBy|OrderByDescending|ThenBy|ThenByDescending|GroupBy|ToDictionary|Except|Intersect|Union)\s*\(\s*(?!\))'; Cat = 'no-comparer-{0}'; Disp = 'needs-triage' }
                @{ Rx = '\.(Distinct)\s*\(\s*\)'; Cat = 'no-comparer-{0}'; Disp = 'needs-triage' }
            )

            foreach ($check in $ruleEightChecks) {
                $m = [regex]::Match($noStr, $check.Rx)
                $hit = $false
                while ($m.Success) {
                    # An ordering key whose member name ends in a rule-2 non-string suffix (`CreatedAt`,
                    # `MsrpPrice`) is not a string ordering: the culture default cannot bite and there is
                    # no StringComparer to pass. `Id` is deliberately absent (OIDC ids are strings).
                    $afterCall = $noStr.Substring($m.Index + $m.Length)
                    if ($afterCall -match '^\s*\w+\s*=>\s*[\w.]*\.(\w+)\s*\)' -and
                        $Matches[1] -match '(Date|Time|At|Count|Amount|Price|Total|Duration)$') {
                        $m = $m.NextMatch()
                        continue
                    }
                    if (-not (Test-CallStatesComparison $noStrWithContinuation $m.Index)) {
                        $name = $m.Groups[1].Value.ToLowerInvariant()
                        Add-Row $repo $relPath $lineNum $area 8 ([string]::Format($check.Cat, $name)) $check.Disp $raw
                        $hit = $true
                        break
                    }
                    $m = $m.NextMatch()
                }
                if ($hit) { break }
            }
        }
    }

    if ($Rule -contains 13) {
        # NullLogger<T>.Instance is the ALLOWED form in tests (TESTING.md), and `\bILogger\b` cannot
        # match inside "NullLogger" - no ILogger substring - so it stays out without a special case.
        if ($noStr -match '\bConsole\.(WriteLine|Write|Error)\b') {
            Add-Row $repo $relPath $lineNum $area 13 'console-write' 'remove-console-write' $raw
        }
        elseif ($noStr -match '\.Log(Information|Warning|Error|Debug|Trace|Critical)\s*\(') {
            $level = $Matches[1]
            # A log call naming an exception is the duplicate-of-the-global-handler case; rule 13 sends
            # it to the span instead, via Telemetry.Tracing.RecordHandledException.
            if ($noStr -match '\b(ex|exception|error)\b') {
                Add-Row $repo $relPath $lineNum $area 13 "log$level-of-exception" 'record-on-activity' $raw
            }
            else {
                Add-Row $repo $relPath $lineNum $area 13 "log$level" 'remove-ad-hoc-logging' $raw
            }
        }
        # Removing the logging service is the OPPOSITE of ad-hoc logging, and a DI registration is not a
        # log call at all. `services.RemoveAll<ILoggerFactory>()` reported as a rule 13 violation against a
        # line whose whole purpose is to take logging out.
        elseif ($noStr -match '\b(?:RemoveAll|Remove|Replace)\s*<\s*ILoggerFactory\s*>') {
            # No row: nothing here narrates anything.
        }
        # Rule 13 bans the code NARRATING, and a constructor parameter that only satisfies `base(...)`
        # narrates nothing: there is no logger in this class to call. What decides it is whether OUR class
        # KEEPS it - the moment the parameter is assigned to an instance member the class holds a logger of
        # its own, and rule 13 applies again in full. See Get-BasePassThroughLoggerParameters.
        elseif ($noStr -match '\bILogger(?:Factory)?(?:\s*<[^>]*>)?\s+(\w+)' -and
                $mandatedLoggerParameters.Contains($Matches[1])) {
            # No row: the base constructor demands the parameter and nothing in this class holds on to it.
        }
        elseif ($noStr -match '\bILoggerFactory\b') {
            Add-Row $repo $relPath $lineNum $area 13 'iloggerfactory' 'remove-ad-hoc-logging' $raw
        }
        # A test's ILogger only exists because a production constructor still demands one, so it is not a
        # separate disposition - it disappears when the production dependency does. Emitting
        # 'use-nulllogger' here would send a reader off to add NullLogger<T>.Instance everywhere, which
        # rule 13 then requires them to undo. Same removal, sequenced after the production side.
        elseif ($noStr -match '\bILogger\b') {
            Add-Row $repo $relPath $lineNum $area 13 'ilogger-dependency' 'remove-ad-hoc-logging' $raw
        }
    }

    # `!(?!=)` keeps `!=` out. The preceding class is `[\w)\]]`, not `\w`, so a bang on a CALL result
    # (`GetString()!`) or an INDEX result (`items[0]!`) is caught. It used to be `\w` alone, which meant
    # `route.paramMap.get('id')!` produced no row at all and a zero-row rule 9 scan read as compliance -
    # four live violations were invisible to it. `!flag` and `!(cond)` still cannot match, because `(`
    # and whitespace are outside the class.
    if ($Rule -contains 9) {
        if ($noStr -match $script:NullForgivingPattern) {
            Add-Row $repo $relPath $lineNum $area 9 'null-forgiving' 'narrow-the-type' $raw
        }
    }
}

# TypeScript gets its OWN scanner rather than a call into Scan-CSharpLine: almost every pattern there is
# C#-specific (string.Empty, DateTimeOffset, IEnumerable<T>?, ILogger) and would misfire on TS syntax. Only
# rule 1 is language-neutral enough to run, and only in the four positions where the empty string is the
# BOUND VALUE or the RETURN. An empty string in call-argument position - parts.join(''), word.split(''),
# value.replace(/x/g, '') - is the idiomatic JS way to spell "no separator" and is not a rule-1 violation.
$tsEmptyString = '(?:''''|""|``)'

# Remove-LiteralBodies' single-quote arm is a C# CHAR literal, so it leaves multi-character TS strings
# whole. The `+` and the sentinel are both load-bearing: a genuinely empty `''` has no body to match and
# survives, while every non-empty literal collapses to '~' and can no longer spell an empty string inside.
function Remove-TsLiteralBodies([string]$line) {
    $out = [regex]::Replace($line, '`(?:\\.|[^`\\])+`', '`~`')
    $out = [regex]::Replace($out, '"(?:\\.|[^"\\])+"', '"~"')
    $out = [regex]::Replace($out, "'(?:\\.|[^'\\])+'", "'~'")
    return $out
}

function Scan-TypeScriptLine($repo, $relPath, $lineNum, $area, $raw) {
    if (($Rule -notcontains 1) -and ($Rule -notcontains 9)) { return }
    $code = Remove-TsLiteralBodies (Remove-LineComment $raw)
    if ($code.Trim() -eq '') { return }

    # Rule 9 binds every language, and TypeScript is where the shape actually concentrates: the idiom
    # reached for most is a bang on a call result (`route.paramMap.get('id')!`). This scanner used to
    # return early unless rule 1 was requested, so rule 9 never ran on `.ts`/`.tsx` at all - not a blind
    # spot in the pattern, an absent scan. Literal bodies are already collapsed above, so a `!` inside a
    # string cannot reach here.
    if ($Rule -contains 9) {
        if ($code -match $script:NullForgivingPattern) {
            Add-Row $repo $relPath $lineNum $area 9 'null-forgiving' 'narrow-the-type' $raw
        }
    }

    if ($Rule -notcontains 1) { return }

    if ($code -match "\?\?\s*$tsEmptyString") {
        Add-Row $repo $relPath $lineNum $area 1 'null-coalesce-to-empty' 'use-null' $raw
    }
    elseif ($code -match "\breturn\s+$tsEmptyString\s*;?") {
        Add-Row $repo $relPath $lineNum $area 1 'returns-empty-string' 'use-null' $raw
    }
    elseif ($code -match "=>\s*$tsEmptyString\s*[;,)]?") {
        Add-Row $repo $relPath $lineNum $area 1 'returns-empty-string' 'use-null' $raw
    }
    # The lookbehind keeps `x === ''` out: a comparison against the empty string is rule 8's shape, and
    # rule 1 is about PRODUCING one.
    elseif ($code -match "(?<![=!<>])=\s*$tsEmptyString\s*[;,)]") {
        Add-Row $repo $relPath $lineNum $area 1 'assigned-empty-string' 'use-null' $raw
    }
}

# Rule 11 in TypeScript tests: test data is generated, never a magic string or number, in every language.
# A literal reports unless the position it sits in names something outside the test: a module specifier, a
# test title, an assertion message, a selector, a URL, an attribute name, a `typeof` operand, an error
# message. A genuine `*-constants.ts` owner and a pin against a production constant are the owner itself.
$tsStructuralLiteralPrefix = '(?:\b(?:import|export)\b[^;]*\bfrom\s*|^\s*\}\s*from\s*|\bimport\s*\(\s*|\brequire\s*\(\s*|' +
    '\bconsole\.(?:log|info|warn|error)\s*\(\s*|\bprocess\.(?:stdout|stderr)\.write\s*\(\s*|\bassert\.ok\s*\([^,]*,\s*|' +
    '\b(?:prefixLocator|pickFromPrefix|hasPrefix)\s*\([^,()]*,\s*|' +
    '\b(?:vi|jest)\.(?:mock|doMock|importActual|requireActual|stubGlobal)\s*\(\s*|' +
    '\b(?:test|it|describe|beforeEach|afterEach|beforeAll|afterAll)(?:\.\w+)*\s*\(\s*|' +
    '\b(?:Given|When|Then|Step)\s*\(\s*|' +
    '\b(?:test|it|describe)(?:\.\w+)*\.each\s*\(.*?\)\s*\(\s*|^\s*\]\s*\)\s*\(\s*|' +
    '\b(?:vi|jest)\.spyOn\s*\(.*,\s*|\.setInput\s*\(\s*|\bcreateElement(?:NS)?\s*\(\s*|' +
    '\.tagName\b.*\.(?:toBe|toEqual)\s*\(\s*|\bselector\s*:\s*|' +
    '\b(?:test|it|describe)\.(?:skip|fixme|fail)\s*\([^,()]*(?:\([^()]*\))?[^,()]*,\s*|' +
    '\b\w*ById\s*\(\s*(?:[\w.$]+\s*,\s*)?|\bBy\.css\s*\(\s*|' +
    '\b(?:expectOne|expectNone)\s*\(\s*|' +
    '[\w$)\]]\s*\[\s*|' +
    '\b[A-Z]\w*\s*<[^<>()=;{}]*$|' +
    '\.(?:on|once|off|addEventListener|removeEventListener|waitForEvent|waitForLoadState|press|grantPermissions)\s*\(\s*\[?\s*|' +
    '\bnew\s+(?:Custom|Keyboard|Mouse|Focus|Input)?Event\s*\(\s*|\bwaitUntil\s*:\s*|\.type\(\)\s*[!=]==?\s*|' +
    '\bgetPropertyValue\s*\(\s*|\b\w*[Ss]tyleOf\s*\((?:[^(),]*,\s*)*|' +
    '\bexpect(?:\.soft)?\s*\([^,]*,\s*|' +
    '\b(?:locator|waitForSelector|querySelector(?:All)?|getElementById|getByTestId|closest|matches)\s*(?:<[^<>()]*>)?\s*\(\s*|' +
    '\bObject\.defineProperty\s*\([^,()]*,\s*|' +
    '\b(?:goto|waitForURL|toHaveURL|route|unroute)\s*\(\s*|' +
    '\b(?:toHaveAttribute|getAttribute|setAttribute|hasAttribute|removeAttribute)\s*\(\s*|' +
    '\btypeof\s+[\w.$?\[\]]+\s*[!=]==?\s*|' +
    '\bnew\s+(?:Error|TypeError|RangeError)\s*\(\s*|\bthrow\s+)$'
$tsQuotedLiteral = [regex]::new("'(?:\\.|[^'\\\r\n])*'|""(?:\\.|[^""\\\r\n])*""")
$tsNumericLiteral = [regex]::new('(?<![\w.$\[])-?\d[\d_]*(?:\.\d+)?(?![\w\]])')
$tsRegexLiteral = [regex]::new('(?<=(?:^|[(,=:\[!&|?{};]|\breturn)\s*)/(?![/*])(?:\\.|\[(?:\\.|[^\]\\\r\n])*\]|[^/\\\[\r\n])+/[dgimsuyv]*')

function Update-TsExpectDepth([string]$code) {
    $blankedCode = $tsQuotedLiteral.Replace($code, "''")
    $start = 0
    if ($script:TsExpectDepth -le 0) {
        $open = [regex]::Match($blankedCode, '\bexpect(?:\.soft)?\s*\(')
        if (-not $open.Success) { return }
        $start = $open.Index + $open.Length - 1
        $script:TsExpectDepth = 0
    }
    for ($i = $start; $i -lt $blankedCode.Length; $i++) {
        $character = $blankedCode[$i]
        if ($character -eq '(' -or $character -eq '[' -or $character -eq '{') { $script:TsExpectDepth++ }
        elseif ($character -eq ')' -or $character -eq ']' -or $character -eq '}') {
            $script:TsExpectDepth--
            if ($script:TsExpectDepth -le 0) { $script:TsExpectDepth = 0; return }
        }
    }
}

# Where a line starts relative to backtick templates that span lines: T is template text, I is a `${`
# interpolation and B a brace nested inside one. A template can open inside another's interpolation, so a
# single inside/outside flag misreads the nested template's line as text.
function Test-TsInsideTemplate {
    return $script:TsTemplateStack.Count -gt 0 -and $script:TsTemplateStack[$script:TsTemplateStack.Count - 1] -eq 'T'
}

function Update-TsTemplateStack([string]$code) {
    $stack = $script:TsTemplateStack
    $quote = [char]0
    for ($i = 0; $i -lt $code.Length; $i++) {
        $c = $code[$i]
        if ($stack.Count -gt 0 -and $stack[$stack.Count - 1] -eq 'T') {
            if ($c -eq '\') { $i++ }
            elseif ($c -eq '`') { $stack.RemoveAt($stack.Count - 1) }
            elseif ($c -eq '$' -and $i + 1 -lt $code.Length -and $code[$i + 1] -eq '{') { $stack.Add('I'); $i++ }
            continue
        }
        if ($quote -ne [char]0) {
            if ($c -eq '\') { $i++ }
            elseif ($c -eq $quote) { $quote = [char]0 }
            continue
        }
        if ($c -eq "'" -or $c -eq '"') { $quote = $c }
        elseif ($c -eq '`') { $stack.Add('T') }
        elseif ($c -eq '{' -and $stack.Count -gt 0) { $stack.Add('B') }
        elseif ($c -eq '}' -and $stack.Count -gt 0) { $stack.RemoveAt($stack.Count - 1) }
    }
}

function Scan-TsTestLiteralLine($repo, $relPath, $lineNum, $area, $raw) {
    if ($script:CurrentFileIsTsConstantsOwner) { return }
    $openedInsideTemplate = Test-TsInsideTemplate
    $code = if ($openedInsideTemplate) { (Remove-LineComment ('`' + $raw)).Substring(1) } else { Remove-LineComment $raw }
    if ($code.Trim() -eq '') { return }
    # A line that opens inside a template spanning lines is read as if the backtick had opened on it.
    Update-TsTemplateStack $code
    if ($openedInsideTemplate) { $code = '`' + $code }
    # A call wrapped after its opening parenthesis puts the argument on a line of its own, where the
    # position prefix is on the line before; a literal that opens its line is judged against that line.
    $previous = $script:TsPreviousCode
    $script:TsPreviousCode = $code
    $wrappedPrefix = if ($previous) { $previous.TrimEnd() + ' ' } else { '' }
    # A message wrapped onto lines of its own opens inside an open `expect(` after the value's comma, or
    # right after `new Error(`, and a message built by concatenation carries on through every line that
    # opens on `+`, or follows a message line that ends on `+`.
    $startsWithText = $code.TrimStart() -match "^(?:\+\s*)?['""``]"
    $previousTrimmed = if ($previous) { $previous.TrimEnd() } else { '' }
    $opensMessage = ($script:TsExpectDepth -eq 1 -and $previousTrimmed.EndsWith(',')) -or
        $previousTrimmed -match '\bnew\s+(?:Error|TypeError|RangeError)\s*\(\s*$'
    $continuesMessage = $script:TsPreviousWasExpectMessage -and
        ($code.TrimStart().StartsWith('+') -or $previousTrimmed.EndsWith('+'))
    $isExpectMessage = $startsWithText -and ($opensMessage -or $continuesMessage)
    $script:TsPreviousWasExpectMessage = $isExpectMessage
    Update-TsExpectDepth $code
    if ($isExpectMessage) { return }
    foreach ($m in $tsQuotedLiteral.Matches($code)) {
        $body = $m.Value.Substring(1, $m.Value.Length - 2)
        if ($body -notmatch '[A-Za-z0-9]') { continue }
        # An escape sequence (`'\n'`, `'\t'`) spells punctuation with a letter, so a literal made only of escapes
        # and punctuation is a separator like `', '`, not a datum.
        if (($body -replace '\\(?:[nrtbfv0\\''"`]|x[0-9a-fA-F]{2}|u[0-9a-fA-F]{4})', '') -notmatch '[A-Za-z0-9]') { continue }
        # The string form of a number the numeric sweep allows is allowed for the same reason, rendered as a
        # percentage too.
        if ($body -match '^(?:0|1|-1)%?$') { continue }
        # An id selector is what rule 10 mandates, whichever helper it is handed to, and a CSS custom
        # property is a stylesheet's identifier; neither is test data. The same narrow shape the C# arm
        # accepts: ENTIRELY `#id` or an `[id...=...]` attribute selector, never an id with a descendant.
        if ($body -match '^#[A-Za-z][\w-]*$' -or $body -match '^\[id[\^$*~|]?=[^\]]*\]$' -or $body -match '^--[a-z][\w-]*$') { continue }
        $before = $code.Substring(0, $m.Index)
        # A quote inside a backtick template is part of that template's text, not a literal of its own.
        if (([regex]::Matches($before, '`')).Count % 2 -eq 1) { continue }
        if ($before -match $tsStructuralLiteralPrefix) { continue }
        if ($before.Trim() -eq '' -and $wrappedPrefix -match $tsStructuralLiteralPrefix) { continue }
        # A title handed to `test` imported under another name (`import { test as setup }`) is a test title.
        if ($script:TsTestTitleAliases.Count -gt 0 -and
            $before -match ('\b(?:' + (($script:TsTestTitleAliases | ForEach-Object { [regex]::Escape($_) }) -join '|') + ')(?:\.\w+)*\s*\(\s*$')) { continue }
        # The `message` option of an expect.poll (or expect) call is an assertion message, written inline or
        # on its own line under the options object's opening brace.
        if ($before -match '(?:\bexpect(?:\.soft)?|\.poll)\s*\(.*\{\s*message\s*:\s*$') { continue }
        if ($before -match '^\s*message\s*:\s*$' -and $previousTrimmed -match '(?:\bexpect(?:\.soft)?|\.poll)\s*\(.*\{$') { continue }
        # A synthetic walker's action label names a step for the walk's own report, like a test title.
        if ($relPath -match '[\\/]synthetic[\\/]' -and $before -match '^\s*name\s*:\s*$') { continue }
        # In an ESLint RuleTester case, `code` is the source under lint (the case under test), `name` is a
        # title, `options` must name what its own `code` uses, and languageOptions values are ESLint's enums.
        if ($script:TsFileUsesRuleTester -and $before -match '(?:\b(?:code|name|options|ecmaVersion|sourceType)\s*:.*|\.run\s*\(\s*)$') { continue }
        # A const the same file concatenates into its cases is scaffolding for the source under lint.
        if ($script:TsFileUsesRuleTester -and $before -match '^\s*const\s+(\w+)\s*=\s*$') {
            $scaffoldName = [regex]::Escape($Matches[1])
            if ($script:TsFileText -match "(?:\+\s*$scaffoldName\b|\b$scaffoldName\s*\+)") { continue }
        }
        $after = $code.Substring($m.Index + $m.Length)
        # A relative path resolved against the module's own URL locates a file beside the code, the same
        # standing as a module specifier; any other base, or an absolute URL, is still data.
        if ($body -match '^\.\.?/' -and $before -match '\bnew\s+URL\s*\(\s*$' -and
            $after -match '^\s*,\s*import\.meta\.url\s*\)') { continue }
        if (Test-CopyIsPin '.ts' $before $after) { continue }
        Add-Row $repo $relPath $lineNum $area 11 'magic-string' 'generate' $raw
        return
    }
    $blanked = $tsQuotedLiteral.Replace($code, "''")
    # A regular-expression literal's quantifiers and ranges are pattern syntax, not numbers.
    $blanked = $tsRegexLiteral.Replace($blanked, '/re/')
    # A length or bound handed to the shared generator is that generator's domain, as `Generated.X(8)` is in C#.
    if ($script:TsGeneratorNames -and $script:TsGeneratorNames.Count -gt 0) {
        $callee = ($script:TsGeneratorNames | ForEach-Object { [regex]::Escape($_) }) -join '|'
        $blanked = [regex]::Replace($blanked, "(?<![\w.])((?:$callee)\s*\()[^()]*\)", '$1)')
    }
    foreach ($m in $tsNumericLiteral.Matches($blanked)) {
        if ($m.Value -in @('0', '1', '-1')) { continue }
        $before = $blanked.Substring(0, $m.Index)
        if (([regex]::Matches($before, '`')).Count % 2 -eq 1) { continue }
        if ($before -match $tsStructuralLiteralPrefix) { continue }
        if ($before -match '\b(?:randomInt|Math\.random\(\)\s*\*)\s*\(?[^)]*$') { continue }
        if ($before -match '\bparseInt\s*\([^,()]*,\s*$') { continue }
        # A number pinned against a named production constant is the pin rule 11 mandates, as in C#.
        if (Test-CopyIsPin '.ts' $before '') { continue }
        if ($script:TsFileUsesRuleTester -and $before -match '\b(?:ecmaVersion|options)\s*:.*$') { continue }
        # An HTTP status code is RFC 9110's, so it is borrowed from a vendor owner, never generated.
        $statusCode = 0
        if ([int]::TryParse($m.Value, [ref]$statusCode) -and $statusCode -ge 100 -and $statusCode -le 599 -and $code -match '(?i)status') {
            Add-Row $repo $relPath $lineNum $area 11 'http-status-code' 'borrow-from-production' $raw
            return
        }
        Add-Row $repo $relPath $lineNum $area 11 'magic-number' 'generate' $raw
        return
    }
}

function Scan-SelectorLine($repo, $relPath, $lineNum, $area, $raw, $ext) {
    $code = Remove-LineComment $raw
    if ($code.Trim() -eq '') { return }
    # A module specifier names a file, never DOM: `from './leaflet-constants.js'` is not leaflet's markup.
    if ($code -match '^\s*(?:import|export)\b.*\bfrom\s*[''"]' -or $code -match '^\s*\}\s*from\s*[''"]') { return }

    if ($code -match 'leaflet-|mapboxgl-|ol-viewport|\.ng-|cdk-overlay|mat-') {
        Add-Row $repo $relPath $lineNum $area 10 'third-party-generated-dom' 'third-party-dom' $raw
        return
    }
    if ($code -match 'nth-child|first-child|last-child|nth-of-type') {
        Add-Row $repo $relPath $lineNum $area 10 'positional-selector' 'use-id' $raw
        return
    }
    if ($code -match 'xpath=|XPathSelector') {
        Add-Row $repo $relPath $lineNum $area 10 'xpath-selector' 'use-id' $raw
        return
    }
    if ($code -match 'has-text\(|:text\(') {
        Add-Row $repo $relPath $lineNum $area 10 'text-selector' 'use-id' $raw
        return
    }
    # Playwright's hasText option binds a locator to copy - the same defect has-text() names - and the
    # CSS-string patterns above cannot see it. BOTH spellings have to be here: C# passes it as an object
    # initialiser, `PageLocatorOptions { HasText = ... }`, while TypeScript passes an object literal,
    # `.filter({ hasText: 'Elden Ring' })`. An `=`-only pattern reads the TypeScript form as compliant, and
    # did: seven such selectors in Librarian\e2e\library.spec.ts scored zero rule-10 rows for that repo.
    # That is the second time this rule has reported zero against a file that was not clean - see the
    # getBy* note in the header - so a new locator idiom is the thing to check first when a repo's rule-10
    # count reaches zero.
    if ($code -match '(?i)\bhas(Not)?Text(Regex)?\s*[:=]') {
        Add-Row $repo $relPath $lineNum $area 10 'text-selector' 'use-id' $raw
        return
    }
    # Playwright's getBy* locators bind to copy directly, which is the shape rule 10 names as
    # `has-text(...)`. Every pattern below this point requires a literal locator/querySelector call, so
    # without these two a whole idiom - and the dominant one in a modern Playwright suite - is invisible.
    # getByRole is flagged only when it carries a `name`: a bare getByRole('main') selects a landmark,
    # which tracks document structure rather than copy. getByTestId is deliberately absent - a test id is
    # a durable authored hook, not a class, a position or an XPath.
    if ($code -match '(?i)\bgetbyrole\s*\(' -and $code -match '(?i)\bname\s*[:=]') {
        Add-Row $repo $relPath $lineNum $area 10 'copy-selector' 'use-id' $raw
        return
    }
    if ($code -match '(?i)\bgetby(text|label|placeholder|title|alttext)\s*\(') {
        Add-Row $repo $relPath $lineNum $area 10 'copy-selector' 'use-id' $raw
        return
    }
    if ($code -match "href\*=|href\^=") {
        Add-Row $repo $relPath $lineNum $area 10 'href-selector' 'use-id' $raw
        return
    }
    # A locator argument beginning with `.` is a class selector. `input[name=...]` is rule 10's one
    # documented exception (ASP.NET derives name and id from the same property path) and is not flagged.
    if ($code -match '(?i)\b(locator|waitforselector|queryselector(all)?)\s*\(\s*[$@]?["'']\s*[a-z0-9\-\[\]= ]*\.[a-z]') {
        Add-Row $repo $relPath $lineNum $area 10 'class-selector' 'use-id' $raw
        return
    }
    # A bare element name as the whole selector tracks markup and copy. The quote must close straight
    # after the tag, so rule 10's documented `input[name='Input.Email']` exception is not matched.
    if ($code -match '(?i)\b(locator|waitforselector|queryselector(all)?)\s*\(\s*[$@]?["''](a|button|div|form|h[1-6]|input|li|ol|p|span|table|tbody|td|th|tr|ul)["'']') {
        Add-Row $repo $relPath $lineNum $area 10 'tag-selector' 'use-id' $raw
        return
    }
    # A BARE prefix match is deliberately NOT flagged - TESTING.md's selector-strategy section prescribes
    # `page.locator('[id^="library-title-"]')` for selecting a set. Position on that set still is.
    if (Test-PositionalSelector $code $ext) {
        Add-Row $repo $relPath $lineNum $area 10 'positional-selector' 'use-id' $raw
    }
}

# A local this file assigns straight from a generator holds a value the test minted, so it has no wording to
# drift and matches no production text: asserting it on its container is the TESTING.md shape, and a negative
# assertion on it is the only way to prove a secret never renders. Provenance, not the local's name, decides.
# A member assigned the same way counts too: a Gherkin step hands a value it minted to a later step through a
# shared context object (`ctx.savedKey = randomUUID()`), since a local cannot outlive its step.
$generatedTarget = '(?:\b(?:const|let|var)\s+(?<name>\w+)|(?<![\w.])(?<name>\w+\.\w+))'
$generatedLocalPattern = "(?m)$generatedTarget\s*=\s*(?:await\s+)?(?:crypto\.)?(?:randomUUID\(\)|randomInt\(|Guid\.NewGuid\(\)|Generated\.New\w*\(|Random\.Shared\.Next)"

function Get-GeneratedLocalNames([string]$text) {
    $names = New-Object 'System.Collections.Generic.HashSet[string]' ([StringComparer]::Ordinal)
    foreach ($m in [regex]::Matches($text, $generatedLocalPattern)) { [void]$names.Add($m.Groups['name'].Value) }
    # The fleet's TypeScript generator is whatever this file imports from `@crgolden/modules/testing`, anchored on
    # the import rather than on a name shape, so a same-named local helper earns nothing.
    $script:TsGeneratorNames = Get-TsGeneratorImportNames $text
    if ($script:TsGeneratorNames.Count -gt 0) {
        $callee = ($script:TsGeneratorNames | ForEach-Object { [regex]::Escape($_) }) -join '|'
        foreach ($m in [regex]::Matches($text, "(?m)$generatedTarget\s*(?::\s*[\w<>\[\]|\s]+)?=\s*(?:$callee)\s*\(")) {
            [void]$names.Add($m.Groups['name'].Value)
        }
    }
    return , $names
}

function Get-TsGeneratorImportNames([string]$text) {
    $names = New-Object 'System.Collections.Generic.HashSet[string]' ([StringComparer]::Ordinal)
    foreach ($m in [regex]::Matches($text, "import\s*\{([^}]*)\}\s*from\s*['""]@crgolden/modules/testing['""]")) {
        foreach ($part in $m.Groups[1].Value -split ',') {
            $name = ($part -split '\s+as\s+')[-1].Trim()
            if ($name -match '^\w+$') { [void]$names.Add($name) }
        }
    }
    return , $names
}

function Scan-CopyAssertionLine($repo, $relPath, $lineNum, $area, $raw) {
    $code = Remove-LineComment $raw
    $argument = [regex]::Match($code, '(?i)\.\s*(?:toHaveText|toContainText|ToHaveTextAsync|ToContainTextAsync)\s*\(\s*([A-Za-z_]\w*(?:\.\w+)?)\s*[,)]')
    if ($argument.Success -and $script:GeneratedLocalNames -and $script:GeneratedLocalNames.Contains($argument.Groups[1].Value)) {
        return
    }
    if ($code -match '(?i)\.\s*(toHaveText|toContainText|toHaveTitle|ToHaveTextAsync|ToContainTextAsync|ToHaveTitleAsync)\s*\(' -or
        $code -cmatch '\b(textContent|innerText)\b[^;]*\)\s*\.\s*(toBe|toEqual|toContain|toMatch)\s*\(' -or
        $code -cmatch '\bAssert\.(Equal|Contains|StartsWith|EndsWith)\s*\([^;]*\b(TextContentAsync|InnerTextAsync)\s*\(') {
        Add-Row $repo $relPath $lineNum $area 10 'copy-assertion' 'assert-id-or-url' $raw
        return
    }
    if ($code -match '(?i)\b(TextContentAsync|InnerTextAsync|InnerHTMLAsync|textContent|innerText|innerHTML)\s*\(\s*[$@]?["''](body|html)["'']' -or
        $code -match '(?i)\blocator\s*\(\s*[$@]?["''](body|html)["'']\s*\)\s*\.\s*(textContent|innerText|innerHTML|allInnerTexts|allTextContents|TextContentAsync|InnerTextAsync|InnerHTMLAsync|AllInnerTextsAsync|AllTextContentsAsync)\s*\(' -or
        $code -cmatch '\bpage\s*\.\s*(content|ContentAsync)\s*\(\s*\)') {
        Add-Row $repo $relPath $lineNum $area 10 'page-text-read' 'assert-id-or-url' $raw
    }
}

# Rule 17. A timeout in test code is banned whatever its value and whatever it is called, so a named constant
# (`{ timeout: RENDER_TIMEOUT_MS }`) reports exactly like a literal; matching only numbers would let a suite
# clear the rule with one `const`. Literal bodies are blanked first, so a test title or copy that merely
# says "timeout" cannot fire.
#
# A config file is judged differently on purpose. CODE-STYLE.md leaves the runner's whole-test and
# server-start budgets to config, so a bare `timeout:` there is silent; the keys that raise a per-action
# default always report, because a raised default is a timeout on every call.
function Scan-TimeoutLine($repo, $relPath, $lineNum, $area, $raw, $ext) {
    $isCSharp = $ext -eq '.cs'
    $code = if ($isCSharp) { Remove-LiteralBodies (Remove-LineComment $raw) }
            else { Remove-TsLiteralBodies (Remove-LineComment $raw) }
    if ($code.Trim() -eq '') { return }

    if ($area -eq 'test-config') {
        if ($code -match '\b(actionTimeout|navigationTimeout|testTimeout|hookTimeout|teardownTimeout)\s*:' -or
            $code -match '\bexpect\s*:\s*\{[^}]*\btimeout\s*:') {
            Add-Row $repo $relPath $lineNum $area 17 'raised-default-timeout' 'remove-timeout' $raw
        }
        return
    }

    if ($isCSharp) {
        if ($code -match '\bSetDefault(Navigation)?Timeout\s*\(') {
            Add-Row $repo $relPath $lineNum $area 17 'raised-default-timeout' 'remove-timeout' $raw
        }
        # A TimeProvider argument makes the delay a fake-clock tick, the shape TESTING.md prescribes.
        elseif ($code -match '\b(WaitForTimeoutAsync|Thread\.Sleep)\s*\(' -or
                ($code -match '\bTask\.Delay\s*\(' -and $code -notmatch 'timeProvider')) {
            Add-Row $repo $relPath $lineNum $area 17 'wall-clock-sleep' 'await-the-condition' $raw
        }
        elseif ($code -match '\bTimeSpan\??\s+\w*timeout\w*\s*(=|,|\))') {
            Add-Row $repo $relPath $lineNum $area 17 'timeout-parameter' 'remove-timeout' $raw
        }
        # `(?<!\?\s*)` keeps a ternary's `? timeout : other` out of the named-argument arm.
        elseif ($code -match '(?<!\w)Timeout\s*=(?![=>])' -or
                $code -match '(?<!\?\s*)(?<![\w.])timeout\s*:(?!:)' -or
                $code -match '\bnew\s+CancellationTokenSource\s*\(\s*[^)\s]' -or
                $code -match '\.CancelAfter\s*\(' -or
                $code -match '\.WaitAsync\s*\(\s*(TimeSpan\b|[\w.]*timeout)') {
            Add-Row $repo $relPath $lineNum $area 17 'per-call-timeout' 'remove-timeout' $raw
        }
        return
    }

    if ($code -match '\bsetDefault(Navigation)?Timeout\s*\(' -or
        $code -match '\b(test|it|describe)\.(setTimeout|slow)\s*\(' -or
        $code -match '\bvi\.setConfig\s*\(.*timeout') {
        Add-Row $repo $relPath $lineNum $area 17 'raised-default-timeout' 'remove-timeout' $raw
    }
    # A setTimeout that schedules a callback (a mock simulating latency) is behaviour, not a wait; only one
    # wrapped in a Promise on the same line is the sleep idiom.
    elseif ($code -match '\bwaitForTimeout\s*\(' -or $code -match '\bnew\s+Promise\b.*\bsetTimeout\s*\(') {
        Add-Row $repo $relPath $lineNum $area 17 'wall-clock-sleep' 'await-the-condition' $raw
    }
    elseif ($code -match '\b\w*timeout\w*\s*\??\s*:\s*number\b') {
        Add-Row $repo $relPath $lineNum $area 17 'timeout-parameter' 'remove-timeout' $raw
    }
    elseif ($code -match '(?<![\w.])(timeout|actionTimeout|navigationTimeout|testTimeout|hookTimeout)\s*:(?!:)') {
        Add-Row $repo $relPath $lineNum $area 17 'per-call-timeout' 'remove-timeout' $raw
    }
}

function Get-LiteralDisposition([string]$literal, [bool]$isNumeric, [string]$relPath) {
    if ($relPath -match $katFilePattern) {
        if (-not $isNumeric -and $literal.Trim('"') -match $vectorLiteralPattern) {
            return @('known-answer-or-wire-constant', 'keep-kat')
        }
        return @('in-crypto-or-digest-test', 'needs-triage')
    }
    if ($isNumeric) {
        # `0m`, `1L`: the type suffix does not change which number it is, and neither does a zero fractional
        # part - `0.0` and `1.0` are the boundaries `0` and `1` spelled for a double. `20.0` stays a magic
        # number and `0.05` stays a fraction, which the harness controls pin.
        $bare = $literal -replace '[dmfLuUL]+$', '' -replace '^(-?\d+)\.0+$', '$1'
        if ($bare -match '^-?(0|1|2147483647|-2147483648|9223372036854775807)$') {
            return @('boundary-or-sentinel', 'keep-boundary')
        }
        return @('magic-number', 'generate')
    }
    $inner = $literal.Trim('"').Trim()
    if ($inner -eq '') { return @('empty-string-literal', 'needs-triage') }
    # The URL root is the string form of a boundary sentinel: fixed by URL syntax, not chosen.
    if ($inner -eq '/') { return @('boundary-or-sentinel', 'keep-boundary') }
    # An OData system query option or annotation is spelled by the OData specification, not by us, so there
    # is no end state to generate toward - `$top` cannot become `Generated.NewQueryOption()` and still
    # parse. This is a CLOSED LIST from the spec rather than a `^\$` prefix rule, because a prefix rule
    # would also swallow any literal a test happens to start with a dollar sign; the harness pins that
    # difference with `"$notAnODataOption"` asserted MUST-FIRE beside `"$filter"` asserted MUST-NOT.
    if ($inner -cmatch '^\$(filter|orderby|top|skip|count|select|expand|search|apply|format)$' `
            -or $inner -cmatch '^@odata\.\w+$') {
        return @('odata-system-name', 'keep-boundary')
    }
    if ($inner -match $prodIdentifierPattern) { return @('prod-owned-identifier', 'move-to-sut-constant') }
    # Connection strings, URLs, and config keys are operating data the SUT owns even when not in the
    # known-identifier list above.
    # -cmatch, not -match: the suffix list is PascalCase because it describes a CONFIG KEY NAME
    # (`MongoDbHost`, `ServiceBusEndpoint`). Case-insensitively it also swallowed ordinary lower-case
    # words that merely end in one of them - `localhost` matched `Host` and was labelled an identifier
    # production owns, which it is not. The row still fires either way; only its category and its advice
    # were wrong, and 'move it to a SUT constant' is unfollowable when no such constant exists.
    if ($inner -match '^(https?://|[A-Za-z]+://)' -or $inner -cmatch '^[\w.]+(ConnectionString|Endpoint|Uri|Url|Host|Port|Key|Secret)$') {
        return @('prod-owned-identifier', 'move-to-sut-constant')
    }
    if ($inner -match '^[0-9a-fA-F]{16,}$') { return @('hex-vector', 'keep-kat') }
    return @('magic-string', 'generate')
}

# Rule 12 needs to know whether a line sits inside a [Fact]/[Theory] body, because TESTING.md explicitly
# allows a named helper to branch ("the helper may branch, the test may not") and allows `foreach` in
# DisposeAsync teardown. A flat line-level grep would flag both and be useless. So this walks the file
# tracking brace depth from each test method's opening brace.
function Scan-TestLogic($repo, $relPath, $area, $lines) {
    $pendingTestAttr = $false
    $inTest = $false
    # The fleet uses Allman braces, so a method signature line carries no `{` and $depth does not rise
    # until the NEXT line. Without $bodyEntered the exit check below fires on the signature line itself
    # ($depth -le $entryDepth is trivially true there) and every test body is skipped - which is exactly
    # the bug that made this rule report zero against a ground truth of 100.
    $bodyEntered = $false
    $entryDepth = 0
    $depth = 0
    $methodName = ''
    $sigText = ''

    for ($i = 0; $i -lt $lines.Count; $i++) {
        $raw = $lines[$i]
        $lineNum = $i + 1
        $code = Remove-LiteralBodies (Remove-LineComment $raw)
        if ($code.Trim() -eq '') { continue }

        if ($code -match '^\s*\[\s*(Fact|Theory)\b') { $pendingTestAttr = $true }

        $opens = ([regex]::Matches($code, '\{')).Count
        $closes = ([regex]::Matches($code, '\}')).Count

        # A wrapped parameter list leaves no closing paren on the signature line, so matching a call shape
        # per line never recognises the test and the body it guards goes unscanned for logic. Accumulate
        # the signature until its parens balance, skipping the sibling attribute lines ([InlineData(...)])
        # whose own parens would balance it early. Rule 18's scanner carries the same guard, and hit the
        # same defect from the other side: it reported a method named Setup against a Moq call.
        if ($pendingTestAttr -and $code -match '^\s*\[') {
            $depth += $opens - $closes
            continue
        }

        if ($pendingTestAttr) {
            $sigText = ($sigText, $code) -join ' '
            if (([regex]::Matches($sigText, '\(')).Count -eq 0 -or
                ([regex]::Matches($sigText, '\(')).Count -ne ([regex]::Matches($sigText, '\)')).Count) {
                $depth += $opens - $closes
                continue
            }

            if ($sigText -match '\b(\w+)\s*\(') { $methodName = $Matches[1] }
            # Expression-bodied test: one line, no body to track.
            if ($sigText -match '=>' -and $code.TrimEnd().EndsWith(';')) {
                Test-LogicLine $repo $relPath $lineNum $area $methodName $code $raw
                $pendingTestAttr = $false
                $sigText = ''
                $depth += $opens - $closes
                continue
            }
            $pendingTestAttr = $false
            $sigText = ''
            $inTest = $true
            $bodyEntered = $false
            $entryDepth = $depth
        }
        elseif ($inTest -and $bodyEntered) {
            Test-LogicLine $repo $relPath $lineNum $area $methodName $code $raw
        }

        $depth += $opens - $closes
        if ($inTest -and -not $bodyEntered -and $depth -gt $entryDepth) { $bodyEntered = $true }
        elseif ($inTest -and $bodyEntered -and $depth -le $entryDepth) { $inTest = $false }
    }
}

# Rule 12, second arm: a theory's data rows are its cases, so a row drawn from a generator is a case whose
# condition lives in a random value rather than in a test name. Such a source nearly always mixes several
# conditions (null, empty, "some generated text") into one test, which is rule 12's branching-over-input in
# data form. It is also mechanically unstable: xUnit v3 derives a pre-enumerated row's unique test id from
# the serialized row arguments (UniqueIDGenerator.ForTestCase), so the row gets a new id in every process,
# and Stryker's MTP runner then cannot find the test its coverage recorded and leaves the mutant Pending.
# One row per MemberData source that reads a generator, directly or through a static readonly field this
# file initializes from one. A source nobody names in [MemberData] is an ordinary helper and is not a theory.
function Scan-GeneratedTheoryData($repo, $relPath, $area, $lines) {
    $generatorPattern = 'Generated\.\w+\s*\(|Guid\.NewGuid\s*\(|Random\.Shared\.|RandomNumberGenerator\.'

    $generatedFields = New-Object System.Collections.Generic.List[string]
    foreach ($line in $lines) {
        $code = Remove-LiteralBodies (Remove-LineComment $line)
        $field = [regex]::Match($code, '\bstatic\s+readonly\s+[^=;]*?\b(\w+)\s*=\s*(.+)$')
        if ($field.Success -and $field.Groups[2].Value -match $generatorPattern) {
            $generatedFields.Add($field.Groups[1].Value)
        }
    }

    $sourceNames = New-Object System.Collections.Generic.HashSet[string]
    foreach ($line in $lines) {
        foreach ($m in [regex]::Matches($line, '\[\s*MemberData\s*\(\s*nameof\s*\(\s*(\w+)\s*\)')) {
            [void]$sourceNames.Add($m.Groups[1].Value)
        }
    }

    foreach ($name in $sourceNames) {
        $declPattern = '^\s*(public|internal|private|protected)[^=;(]*\bstatic\b[^=;(]*\b' + [regex]::Escape($name) + '\b\s*(\(\s*\))?\s*(=>|\{|$)'
        for ($i = 0; $i -lt $lines.Count; $i++) {
            $declCode = Remove-LineComment $lines[$i]
            if ($declCode -notmatch $declPattern) { continue }

            $expressionBodied = $declCode -match '=>'
            $depth = 0
            $sawOpen = $false
            $body = New-Object System.Text.StringBuilder
            for ($j = $i; $j -lt $lines.Count; $j++) {
                $code = Remove-LiteralBodies (Remove-LineComment $lines[$j])
                [void]$body.AppendLine($code)
                $opens = ([regex]::Matches($code, '\{')).Count
                $depth += $opens - ([regex]::Matches($code, '\}')).Count
                if ($opens -gt 0) { $sawOpen = $true }
                if ($expressionBodied -and $depth -le 0 -and $code.TrimEnd().EndsWith(';')) { break }
                if (-not $expressionBodied -and $sawOpen -and $depth -le 0) { break }
            }

            $bodyText = $body.ToString()
            $readsGenerator = $bodyText -match $generatorPattern
            $readsGeneratedField = @($generatedFields | Where-Object { $bodyText -match ('\b' + [regex]::Escape($_) + '\b') }).Count -gt 0
            if ($readsGenerator -or $readsGeneratedField) {
                Add-Row $repo $relPath ($i + 1) $area 12 "generated-theory-data-in-$name" 'split-into-facts' $lines[$i]
            }
            break
        }
    }
}

# Rule 18: a test method whose body carries no `// Act` has no separable call under test. The marker is
# read off the RAW line, because Remove-LineComment has already deleted it from $code by the time the
# rule 12 scanner sees a line - which is why this cannot be folded into Test-LogicLine.
function Scan-TestAaa($repo, $relPath, $area, $lines) {
    $pendingTestAttr = $false
    $inTest = $false
    $bodyEntered = $false
    $entryDepth = 0
    $depth = 0
    $methodName = ''
    $methodLine = 0
    $methodRaw = ''
    $sigText = ''
    $sawAct = $false
    $sawCombined = $false
    $pendingArrange = $false

    for ($i = 0; $i -lt $lines.Count; $i++) {
        $raw = $lines[$i]
        $lineNum = $i + 1
        $code = Remove-LiteralBodies (Remove-LineComment $raw)
        if ($code.Trim() -eq '' -and $raw -notmatch '^\s*//') { continue }

        if ($code -match '^\s*\[\s*(Fact|Theory)\b') { $pendingTestAttr = $true }

        $opens = ([regex]::Matches($code, '\{')).Count
        $closes = ([regex]::Matches($code, '\}')).Count

        # A test signature may WRAP its parameter list, in which case the signature line carries no closing
        # paren. Matching a call shape line-by-line instead picks up the first Moq `.Setup(...)` inside the
        # body and reports a method named Setup, while the real test is never seen. So accumulate the
        # signature across lines until its parens balance, skipping the sibling attribute lines
        # ([InlineData(...)]) whose own parens would otherwise balance it early.
        if ($pendingTestAttr -and $code -match '^\s*\[') {
            $depth += $opens - $closes
            continue
        }

        if ($pendingTestAttr) {
            if ($sigText -eq '') {
                $methodLine = $lineNum
                $methodRaw = $raw
            }

            $sigText = ($sigText, $code) -join ' '
            if (([regex]::Matches($sigText, '\(')).Count -eq 0 -or
                ([regex]::Matches($sigText, '\(')).Count -ne ([regex]::Matches($sigText, '\)')).Count) {
                $depth += $opens - $closes
                continue
            }

            if ($sigText -match '\b(\w+)\s*\(') { $methodName = $Matches[1] }
            # An expression-bodied test has no body to divide, so it is the fused shape rule 18 targets.
            if ($sigText -match '=>' -and $code.TrimEnd().EndsWith(';')) {
                Add-Row $repo $relPath $methodLine $area 18 "aaa-missing-in-$methodName" 'add-aaa-markers' $methodRaw
                $pendingTestAttr = $false
                $sigText = ''
                $depth += $opens - $closes
                continue
            }

            $pendingTestAttr = $false
            $sigText = ''
            $inTest = $true
            $bodyEntered = $false
            $entryDepth = $depth
            $sawAct = $false
            $sawCombined = $false
            $pendingArrange = $false
            $markerScopes = @{}
        }
        elseif ($inTest -and $bodyEntered) {
            if ($raw -match '^\s*//\s*Act\s*(&|and)\s*Assert\s*$') {
                $sawCombined = $true
                $pendingArrange = $false
            }
            elseif ($raw -match '^\s*//\s*(?<marker>Arrange|Act|Assert)\s*$') {
                $marker = $Matches['marker']
                if (-not $markerScopes.ContainsKey($depth)) {
                    $markerScopes[$depth] = New-Object 'System.Collections.Generic.List[string]'
                }

                [void]$markerScopes[$depth].Add($marker)
                if ($marker -eq 'Act') {
                    $sawAct = $true
                    if ($pendingArrange) {
                        Add-Row $repo $relPath $methodLine $area 18 "aaa-empty-arrange-in-$methodName" 'remove-empty-arrange' $methodRaw
                    }
                }

                $pendingArrange = $marker -eq 'Arrange'
            }
            else {
                $pendingArrange = $false
            }
        }

        $depth += $opens - $closes
        if ($inTest -and -not $bodyEntered -and $depth -gt $entryDepth) { $bodyEntered = $true }
        elseif ($inTest -and $bodyEntered -and $depth -le $entryDepth) {
            if ($sawCombined) {
                Add-Row $repo $relPath $methodLine $area 18 "aaa-combined-in-$methodName" 'split-act-from-assert' $methodRaw
            }
            elseif (-not $sawAct) {
                Add-Row $repo $relPath $methodLine $area 18 "aaa-missing-in-$methodName" 'add-aaa-markers' $methodRaw
            }

            Resolve-ScopeMarkers $repo $relPath $area $methodName $methodLine $methodRaw $markerScopes $entryDepth
            $inTest = $false
        }
        elseif ($inTest -and $bodyEntered) {
            # A nested scope closing is where its markers are judged, so two sibling lambdas at the same
            # depth are never merged into one another's counts.
            Resolve-ScopeMarkers $repo $relPath $area $methodName $methodLine $methodRaw $markerScopes $depth
        }
    }
}

# Rule 18's duplication and ordering checks count markers PER SCOPE, resetting at every nested brace, and
# that is not a refinement - it is what makes them usable. CODE-STYLE.md rule 18 sanctions a property-based
# test marking inside the sample lambda while the generator sits under an outer `// Arrange`, so a
# body-level count reports every CsCheck test in the fleet. Two sibling lambdas at the same depth must not
# merge either, which is why a level is evaluated and dropped when its brace closes rather than at the end
# of the body.
#
# Both checks exist because the three original rule 18 checks cannot see either shape: a body carrying
# three `// Assert` markers has an Act, is not combined, and has a non-empty Arrange, so it satisfies all
# of them. Rule 18 read zero across the workspace while three real violations stood, one of them a red
# build.
function Resolve-ScopeMarkers($repo, $relPath, $area, $methodName, $methodLine, $methodRaw, $scopes, [int]$keepDepth) {
    $markerRank = @{ 'Arrange' = 1; 'Act' = 2; 'Assert' = 3 }
    foreach ($scopeDepth in @($scopes.Keys | Where-Object { $_ -gt $keepDepth })) {
        $order = $scopes[$scopeDepth]
        if (@($order | Group-Object | Where-Object { $_.Count -gt 1 }).Count -gt 0) {
            Add-Row $repo $relPath $methodLine $area 18 "aaa-duplicate-in-$methodName" 'remove-duplicate-marker' $methodRaw
        }

        $ranks = @($order | ForEach-Object { $markerRank[$_] })
        for ($k = 1; $k -lt $ranks.Count; $k++) {
            if ($ranks[$k] -lt $ranks[$k - 1]) {
                Add-Row $repo $relPath $methodLine $area 18 "aaa-out-of-order-in-$methodName" 'reorder-aaa-markers' $methodRaw
                break
            }
        }

        $scopes.Remove($scopeDepth)
    }
}

function Test-LogicLine($repo, $relPath, $lineNum, $area, $methodName, $code, $raw) {
    if ($code -match '\bif\s*\(') {
        Add-Row $repo $relPath $lineNum $area 12 "if-in-$methodName" 'restructure-split-test' $raw
        return
    }
    if ($code -match '^\s*\}?\s*else\b') {
        Add-Row $repo $relPath $lineNum $area 12 "else-in-$methodName" 'restructure-split-test' $raw
        return
    }
    if ($code -match '\bswitch\s*[\(\{]') {
        Add-Row $repo $relPath $lineNum $area 12 "switch-in-$methodName" 'restructure-split-test' $raw
        return
    }
    if ($code -match '\b(for|foreach|while)\s*\(') {
        Add-Row $repo $relPath $lineNum $area 12 "loop-in-$methodName" 'restructure-use-theory' $raw
        return
    }
    if ($code -match '\b(try\s*\{|catch\s*(\(|\{))') {
        Add-Row $repo $relPath $lineNum $area 12 "try-catch-in-$methodName" 'use-assert-throws' $raw
        return
    }
    # Ternary detection is the least precise of the set - nullable type annotations, `?.` and `??` all
    # use the same character. Requires whitespace on both sides of both operators, and emits
    # needs-triage rather than a directive, so a false positive costs a glance and nothing more.
    if ($code -match '[\w\)\]"'']\s+\?\s+[^:;()]+\s+:\s+') {
        Add-Row $repo $relPath $lineNum $area 12 "ternary-in-$methodName" 'needs-triage' $raw
    }
}

# Rule 12 in TypeScript: the same walk as Scan-TestLogic, opened by an it()/test() call instead of an
# attribute. Only the modifiers that still declare one test extend the call (`it.each`, `test.only`), so
# `test.describe`, `test.beforeEach`, `test.use` and `test.step` never open a body. The body starts on the
# first line after the call whose braces net upward AND which carries `=>` or `function`, so a multi-line
# it.each row, an object literal, never reads as the callback. `??` is branching here as the rule says; a
# named helper outside the callback may still branch, and `.map`/`.filter`/`.some` are not flagged, matching
# the C# arm's treatment of LINQ.
$tsTestCallPattern = '^\s*(?:it|test)(?:\.(?:each|only|skip|todo|concurrent|fails|sequential|failing))*\s*[\(`]'

function Scan-TsTestLogic($repo, $relPath, $area, $lines) {
    $pending = $false
    $inBody = $false
    $callDepth = 0
    $bodyBase = 0
    $depth = 0
    $testLine = 0
    for ($i = 0; $i -lt $lines.Count; $i++) {
        $raw = $lines[$i]
        $code = Remove-TsLiteralBodies (Remove-LineComment $raw)
        if ($code.Trim() -eq '') { continue }
        $net = ([regex]::Matches($code, '\{')).Count - ([regex]::Matches($code, '\}')).Count
        $before = $depth
        $depth += $net
        if ($inBody) {
            if ($depth -le $bodyBase) { $inBody = $false; continue }
            Test-TsLogicLine $repo $relPath ($i + 1) $area "test-at-line-$testLine" $code $raw
            continue
        }
        if (-not $pending -and $code -match $tsTestCallPattern) {
            $pending = $true
            $callDepth = $before
            $testLine = $i + 1
        }
        if (-not $pending) { continue }
        if ($net -gt 0 -and $code -match '=>|\bfunction\b') {
            $pending = $false
            $inBody = $true
            $bodyBase = $before
        }
        elseif ($depth -eq $callDepth -and $code -match '\)\s*;?\s*$') {
            $pending = $false
        }
    }
}

function Test-TsLogicLine($repo, $relPath, $lineNum, $area, $testName, $code, $raw) {
    if ($code -match '\bif\s*\(') { Add-Row $repo $relPath $lineNum $area 12 "if-in-$testName" 'restructure-split-test' $raw; return }
    if ($code -match '^\s*\}?\s*else\b') { Add-Row $repo $relPath $lineNum $area 12 "else-in-$testName" 'restructure-split-test' $raw; return }
    if ($code -match '\bswitch\s*\(') { Add-Row $repo $relPath $lineNum $area 12 "switch-in-$testName" 'restructure-split-test' $raw; return }
    if ($code -match '\b(for|while)\s*\(') { Add-Row $repo $relPath $lineNum $area 12 "loop-in-$testName" 'restructure-use-each' $raw; return }
    if ($code -match '\b(try\s*\{|catch\s*(\(|\{))') { Add-Row $repo $relPath $lineNum $area 12 "try-catch-in-$testName" 'use-expect-throw' $raw; return }
    if ($code -match '\?\?') { Add-Row $repo $relPath $lineNum $area 12 "null-coalesce-in-$testName" 'restructure-split-test' $raw; return }
    if ($code -match '[\w\)\]''"]\s+\?\s+[^:;()]+\s+:\s+') { Add-Row $repo $relPath $lineNum $area 12 "ternary-in-$testName" 'needs-triage' $raw }
}

# Rule 12 in Python: a pytest test is a `def test_...` and its body is every following line indented deeper
# than the def. A fixture or helper is any other def and may branch. A statement keyword counts only at
# bracket depth zero, so the `for` and `if` clauses of a generator expression split over several lines are
# not statements. Comprehensions, any() and next() are expression-level iteration, the Python form of the
# LINQ the C# arm leaves alone; `a if c else b` is a ternary wherever it sits, and `with pytest.raises(...)`
# is the Python form of Assert.Throws and stays legal.
function Remove-PyLiteralBodies([string]$line) {
    $out = [regex]::Replace($line, "[rbfuRBFU]{0,2}'(?:\\.|[^'\\])*'", "''")
    $out = [regex]::Replace($out, '[rbfuRBFU]{0,2}"(?:\\.|[^"\\])*"', '""')
    return ($out -replace '#.*$', '')
}

function Scan-PyTestLogic($repo, $relPath, $lines) {
    $testIndent = -1
    $testName = ''
    $inDocstring = $false
    $bracketDepth = 0
    for ($i = 0; $i -lt $lines.Count; $i++) {
        $raw = $lines[$i]
        $quotes = ([regex]::Matches($raw, '"""|''''''')).Count
        if ($inDocstring) {
            if ($quotes % 2 -eq 1) { $inDocstring = $false }
            continue
        }
        if ($quotes % 2 -eq 1) { $inDocstring = $true; continue }
        if ($quotes -gt 0 -or $raw.Trim() -eq '' -or $raw.TrimStart().StartsWith('#')) { continue }
        $code = Remove-PyLiteralBodies $raw
        $statementStart = $bracketDepth -eq 0
        $bracketDepth += ([regex]::Matches($code, '[\[\(\{]')).Count - ([regex]::Matches($code, '[\]\)\}]')).Count
        if ($bracketDepth -lt 0) { $bracketDepth = 0 }
        $indent = $raw.Length - $raw.TrimStart().Length
        if ($statementStart -and $testIndent -ge 0 -and $indent -le $testIndent) { $testIndent = -1 }
        if ($statementStart -and $raw -match '^(\s*)(?:async\s+)?def\s+(test_\w*)\s*\(') {
            $testIndent = $Matches[1].Length
            $testName = $Matches[2]
            continue
        }
        if ($testIndent -lt 0) { continue }
        Test-PyLogicLine $repo $relPath ($i + 1) $testName $code $raw $statementStart
    }
}

function Test-PyLogicLine($repo, $relPath, $lineNum, $testName, $code, $raw, $statementStart) {
    $area = 'test-unit'
    if ($statementStart) {
        if ($code -match '^\s*(if|elif)\b') { Add-Row $repo $relPath $lineNum $area 12 "if-in-$testName" 'restructure-split-test' $raw; return }
        if ($code -match '^\s*else\s*:') { Add-Row $repo $relPath $lineNum $area 12 "else-in-$testName" 'restructure-split-test' $raw; return }
        if ($code -match '^\s*match\s+.+:\s*$') { Add-Row $repo $relPath $lineNum $area 12 "match-in-$testName" 'restructure-split-test' $raw; return }
        if ($code -match '^\s*(?:async\s+)?(for|while)\b') { Add-Row $repo $relPath $lineNum $area 12 "loop-in-$testName" 'restructure-use-parametrize' $raw; return }
        if ($code -match '^\s*(try|except|finally)\b[^:]*:') { Add-Row $repo $relPath $lineNum $area 12 "try-except-in-$testName" 'use-pytest-raises' $raw; return }
    }
    if ($code -match '\S\s+if\s+\S.*\s+else\s+\S') { Add-Row $repo $relPath $lineNum $area 12 "ternary-in-$testName" 'needs-triage' $raw }
}

# Rule 11's naming half: a generated value must be a local whose name states its role in THIS test.
# Two failure modes, detected separately because they need different fixes - a generator inlined into an
# argument (no name at all, so nothing can assert on it) and one parked in a placeholder name.
$weakLocalNamePattern = '^(guid|uuid|id|ident|str|s|n|num|number|value|val|v|x|y|z|i|random|rnd|rand|' +
    'test|temp|tmp|result|res|item|obj|o|data|d|thing|foo|bar|baz|dummy|stub|input|output|expected|actual)\d*$'

# A local declaration head - `var name =` or `Type name =` - captured so the name can be judged.
#
# Deliberately does NOT match a member assignment (`church.DenominationId = Guid.NewGuid();`): the
# type-token alternative requires whitespace then a bare identifier then `=`, and a dotted target has no
# identifier left between the name and the `=`. Member assignment stays an inline finding, because you
# cannot assert against it without re-reading the object you just mutated.
# The type token is optional so that a generator assigned to an already-declared local
# (`traceIdentifier = $"trace-{Guid.NewGuid():N}"`, an `out` parameter) is judged by that name too, and
# access/static/readonly modifiers are allowed so a `private static readonly Guid SharedChatId =
# Guid.NewGuid();` fixture field (a shape rule 11 itself discusses) is judged by its name. A lambda arrow
# (`x => ...`) is excluded with the `==` case.
$localDeclarationPattern = '^\s*(?:(?:private|internal|public|protected|static|readonly)\s+)*(?:(?:var|[A-Za-z_][\w.]*(?:<[^>]*>)?(?:\[\])?\??)\s+)?(\w+)\s*=(?![=>])'

# A generator method's own body (`internal static Guid NewOwnerId() => Guid.NewGuid();`): the method name
# names the value and the caller binds the result, so there is nothing to extract.
$generatorMethodPattern = '^\s*(?:(?:public|internal|private|protected|static)\s+)+[\w<>?\[\],\s]+?\s+\w+\s*\([^)]*\)\s*=>'

$generatorCallPattern = 'Guid\.NewGuid\(\)|Random\.Shared\.Next(?:Double)?\([^)]*\)'

# A generator declared in a test project instead of `Shared.Testing.Generated`. Rule 11 names that one class
# the end state and allows no per-project generator class, so a `New*` method in any test file that draws a
# value, or only forwards to a `Generated` generator under a local name, is a generator in the wrong place:
# other projects cannot share it and each copy drifts on its own. A per-project `TestValues.cs` is no
# exception; it is the shape this row exists to retire. Judged per METHOD, not per line, because a
# block-bodied generator's primitive sits lines below its name. A `New*` method that only assembles objects
# from its arguments or from `Generated` calls is a fixture builder and does not report; the harness pins
# both sides, including that the body scan stops at the method's own end.
$perFileGeneratorHeadPattern = '^\s*(?:(?:private|internal|public|protected|static)\s+)+[\w<>\[\]?,.]+\s+New[A-Z]\w*\s*\('
$perFileGeneratorPrimitivePattern = 'Guid\.NewGuid\(\)|Random\.Shared\.|RandomNumberGenerator\.'
$generatedPassThroughPattern = '=>\s*Generated\.New[A-Z]\w*\s*\(\s*\)\s*;\s*$'

function Scan-PerFileGenerators($repo, $relPath, $area, $lines) {
    for ($i = 0; $i -lt $lines.Count; $i++) {
        if ((Remove-LineComment $lines[$i]) -notmatch $perFileGeneratorHeadPattern) { continue }
        $bodyLines = New-Object System.Collections.Generic.List[string]
        $depth = 0
        $entered = $false
        for ($j = $i; $j -lt $lines.Count; $j++) {
            $code = Remove-LineComment $lines[$j]
            $bodyLines.Add($code)
            $structural = Remove-LiteralBodies $code
            $depth += ([regex]::Matches($structural, '\{')).Count - ([regex]::Matches($structural, '\}')).Count
            if ($depth -gt 0) { $entered = $true }
            if ($entered -and $depth -le 0) { break }
            if (-not $entered -and $structural -match ';\s*$') { break }
        }
        $body = $bodyLines -join "`n"
        if ($body -match $perFileGeneratorPrimitivePattern -or $body -match $generatedPassThroughPattern) {
            Add-Row $repo $relPath ($i + 1) $area 11 'per-file-generator' 'move-to-generated' $lines[$i]
        }
    }
}

# A DEDICATED CONSTANTS CLASS is the fleet's home for a named constant, and it is the end state for a
# literal rather than a finding: rule 11's complaint is an UNNAMED literal at a use site, and naming it
# once in one place is the fix. Flagging the declaration charges the author for complying.
#
# THE FILE MUST EARN THE NAME. If the suffix alone were enough, any row could be silenced by renaming a
# file. So Test-ConstantsOnlyFile reads the file and
# requires every member to be a const or static readonly field: one method, one property, one mutable
# field and it is an ordinary file that happens to end in Constants.cs, and its literals report as usual.
# An attribute argument must be a compile-time constant - the language allows nothing else - so a `const`
# an attribute in the same file names is COMPELLED, not chosen. `[CollectionDefinition(Name)]` beside
# `public const string Name = "Unit"` is the worked example: there is no generated value, no production
# constant and no named local that can stand in, because none of them is a constant expression.
#
# That makes this a predictable, testable state rather than a judgement call, which is why it is taught
# rather than triaged. Deliberately SAME-FILE only: a const referenced by an attribute in some other file
# is not visible here, and widening it to "any const whose name appears in any attribute anywhere" would
# silence values the rule is meant to catch. The control in Test-CodeStyleCatalog.ps1 is a const in the
# very same file that no attribute names, which must still report.
function Get-AttributeReferencedNames([string]$text) {
    $names = New-Object 'System.Collections.Generic.HashSet[string]' ([StringComparer]::Ordinal)
    foreach ($attribute in [regex]::Matches($text, '(?m)^\s*\[\s*\w+\s*\(([^)]*)\)\s*\]')) {
        foreach ($identifier in [regex]::Matches($attribute.Groups[1].Value, '(?<![\w."])([A-Za-z_]\w*)')) {
            [void]$names.Add($identifier.Groups[1].Value)
        }
    }
    return $names
}

# EnvironmentSetting.Count/Percent/Text take the variable name, then the DEFAULT and the BOUNDS that
# frame it. A literal passed as the `default` argument is the config schema's default rather than a
# frozen value, and the bounds beside it are that schema's range; generating any of the three would
# change what the SETTING means rather than what a test proves. Call sites name them as consts, so the
# declaration and the use sit on different lines and the line-anchored receiver arm cannot reach the
# declaration, which is what charged `private const int DefaultStepBudget = 40;` as test data.
#
# Same shape and justification as Get-AttributeReferencedNames: decided once per file, SAME-FILE only, and
# only from lines that actually READ a setting. A const referenced from an ordinary line is not the
# schema and keeps reporting, which is the control the harness pins beside this one. Without that
# second half this collapses into "any const in a file that mentions EnvironmentSetting".
# A DEFAULT PARAMETER VALUE is compelled to be a constant expression by the language, exactly as an
# attribute argument is, so the same reasoning as Get-AttributeReferencedNames applies: the author had
# no choice, and there is no end state other than the const. `ExtractLink(string body, string prefix =
# HttpLinkPrefix)` is the worked example. Deliberately SAME-FILE and deliberately only identifiers that
# appear as a default IN A PARAMETER LIST; a const referenced anywhere else keeps reporting, which is
# the control the harness pins beside it.
function Get-DefaultParameterNames([string]$text) {
    $names = New-Object 'System.Collections.Generic.HashSet[string]' ([StringComparer]::Ordinal)
    $signature = '(?m)^\s*(?:(?:public|internal|private|protected|static|override|virtual|sealed|async|partial|extern|new)\s+)+[\w<>\[\],.?]+\s+\w+\s*\(([^)]*)\)'
    foreach ($match in [regex]::Matches($text, $signature)) {
        foreach ($default in [regex]::Matches($match.Groups[1].Value, '=\s*([A-Za-z_]\w*)')) {
            [void]$names.Add($default.Groups[1].Value)
        }
    }
    return $names
}

function Get-ConfigSchemaConstNames([string]$text) {
    $names = New-Object 'System.Collections.Generic.HashSet[string]' ([StringComparer]::Ordinal)
    foreach ($line in ($text -split "`r?`n")) {
        # A variable NAME handed to the environment API as a const is the same fact the inline-literal arm
        # already accepts there. Only the first argument: a value compared on the same line stays data.
        foreach ($named in [regex]::Matches($line, '(?:Get|Set)EnvironmentVariable\s*\(\s*([A-Za-z_]\w*)\s*[,)]')) {
            [void]$names.Add($named.Groups[1].Value)
        }
        if ($line -notmatch 'EnvironmentSetting\.(?:Count|Percent|Text)\s*\(') { continue }
        foreach ($identifier in [regex]::Matches($line, '(?<![\w."])([A-Za-z_]\w*)')) {
            [void]$names.Add($identifier.Groups[1].Value)
        }
    }
    return $names
}

# A pin test (rule 11: "a test that exists to pin a value asserts the literal") takes a different shape once
# rule 18 splits it. The fused form `Assert.Equal("sub", Policies.SubjectClaimType)` is blanked by the
# qualified-partner arms in Scan-TestLiteralLine; the rule-18 form reads the production value into a local
# under `// Act` and compares the literal to that LOCAL under `// Assert`, so the partner is lower-case and
# those arms cannot see it. Every such row in the fleet was produced by the rule-18 sweep rewriting a pin
# that had never reported, which makes the gap predictable rather than a judgement call.
#
# The tie between the literal and the production value spans statements, so it is decided per file, like
# Get-AttributeReferencedNames. A local is PINNED only when its initialiser is nothing but qualified,
# capitalised member reads - one member, or a collection expression of them. A call, an indexer or a
# lower-case receiver disqualifies it, which is what keeps a computed result compared to a literal
# reporting. The expected side must be a literal, a collection of literals, or a local whose own
# initialiser is exactly that. The local map resets at every [Fact]/[Theory], so a name pinned in one test
# lends nothing to the next. The harness pins all three accepted shapes beside three that must still fire.
function Get-PinnedLiteralLines([string[]]$lines) {
    $pinnedLines = New-Object 'System.Collections.Generic.HashSet[int]'
    $member = '[A-Z]\w*(?:\.\w+)+'
    $literal = '(?:"(?:\\.|[^"\\])*"|-?\d[\d_]*(?:\.\d+)?[dmfLuU]*)'
    $memberSource = "^(?:$member|\[\s*$member(?:\s*,\s*$member)*\s*,?\s*\])$"
    $literalSource = "^(?:$literal|\[\s*$literal(?:\s*,\s*$literal)*\s*,?\s*\])$"
    $pinnedLocals = New-Object 'System.Collections.Generic.HashSet[string]' ([StringComparer]::Ordinal)
    $literalLocals = New-Object 'System.Collections.Generic.Dictionary[string,int[]]' ([StringComparer]::Ordinal)
    $statement = New-Object System.Text.StringBuilder
    $statementStart = 0

    for ($i = 0; $i -lt $lines.Count; $i++) {
        $code = (Remove-LineComment $lines[$i]).Trim()
        if ($code -match '^\[\s*(?:Fact|Theory)\b') {
            $pinnedLocals.Clear()
            $literalLocals.Clear()
            [void]$statement.Clear()
            continue
        }
        if ($code -eq '') { continue }
        if ($statement.Length -eq 0) { $statementStart = $i + 1 }
        [void]$statement.Append(' ').Append($code)
        if ($code -notmatch ';$') {
            if ($code -match '[{}]$') { [void]$statement.Clear() }
            continue
        }

        $text = $statement.ToString().Trim()
        [void]$statement.Clear()
        $span = [int[]]($statementStart..($i + 1))

        if ($text -match '^(?:var|[\w.<>?\[\]]+)\s+(\w+)\s*=\s*(.+?)\s*;$') {
            $name = $Matches[1]
            $source = $Matches[2]
            if ($source -match $memberSource) { [void]$pinnedLocals.Add($name) }
            elseif ($source -match $literalSource) { $literalLocals[$name] = $span }
            continue
        }

        if ($text -match '^Assert\.Equal\s*(?:<[^>]*>)?\s*\(\s*(.+?)\s*,\s*(\w+)\s*\)\s*;$' -and
            $pinnedLocals.Contains($Matches[2])) {
            $expected = $Matches[1]
            if ($expected -match $literalSource) {
                foreach ($n in $span) { [void]$pinnedLines.Add($n) }
            }
            elseif ($literalLocals.ContainsKey($expected)) {
                foreach ($n in $literalLocals[$expected]) { [void]$pinnedLines.Add($n) }
            }
        }
    }

    return , $pinnedLines
}

# A receiver named *sql/*CommandText, reached through member access, indexers, calls and null-conditional
# access (`Commands()[0].CapturedCommandText`, `Assert.Single(commands).ExecutedSql`). The anchor is still
# the suffix: a path ending anywhere else does not match.
$sqlReceiverPath = '[\w.?]*(?:(?:\[[^\]]*\]|\((?:[^()]|\([^()]*\))*\))[\w.?]*)*(?:[Ss]ql|CommandText)\b'
$sqlStatementOpening = '^[$@]{0,2}"\s*(?:SELECT|INSERT INTO|UPDATE|DELETE FROM|FROM|WITH)\s'

# A receiver named *Url/*Uri, reached the same way $sqlReceiverPath reaches a *sql/*CommandText one
# (`page.Url`, `verifyPage.Url`, `new Uri(page.Url).Host`). The URL a browser landed on is the route under
# test, exactly as a captured statement is the query under test, so the expected side of an assertion
# against it is contract text rather than a datum. Same anchor discipline: the SUFFIX decides, so the same
# literal asserted against any other receiver keeps reporting, and the harness pins that control.
# `[Uu]rl`/`[Uu]ri`, not `Url`/`Uri`: [regex]::Replace is CASE-SENSITIVE (unlike -match), so a capitalised
# spelling alone matches `page.Url` and misses the lambda parameter in
# `WaitForURLAsync(url => url.Contains("/Account/Login", ...))`. The *sql sibling spells it `[Ss]ql` for
# exactly this reason; copying that shape without its character class is how the arm half-worked.
# The `(?<!\w)` before the token is what separates a route from a URL-valued datum, and it is the whole
# difference between this arm and a suppression. A bare `Url`/`Uri` reached through a dot is the location
# the browser or request is AT; `user.AvatarUrl` and `store.MockManualUrl` are a domain word with Url glued
# on, and they hold exactly the kind of specimen rule 11 exists to catch. Anchoring on the suffix alone
# could not tell them apart, so every `*Url`-named property went quiet. `RequestUri` is spelled out because
# it is a bare BCL location property that the lookbehind would otherwise reject.
$urlReceiverPath =
    '[\w.?]*(?:(?:\[[^\]]*\]|\((?:[^()]|\([^()]*\))*\))[\w.?]*)*(?<!\w)(?:[Uu]rl|[Uu]ri|RequestUri)\b(?:\.\w+)*'

# THE THREE POSITIONS IN A BROWSER OR HTTP TEST THAT NAME SOMETHING OUTSIDE THE TEST.
#
# The SQL arms above already exempt a statement on the grounds that it is the CONTRACT under test rather
# than a datum fed to the subject. A selector, a route and a wire key are that same thing one tier out, and
# before 2026-09-19 nothing said so: every blanking arm in Scan-TestLiteralLine was anchored to a unit-test
# token (Assert.*, new *Exception(, Parameters[, CommandText), so when rule 11 widened past test-unit the
# E2E area met a sweep with no arm that could fire. It reported 28.3 rows per 100 code lines against the
# unit area's 0.006 on the same rule and the same language - one line in 2.4 of AccountManagementTests.cs -
# and what it reported was `ClickAsync("#disable-2fa-submit")`, `case "GET":` and `AddWithValue("@Id", id)`.
# A sweep that returns nearly every literal in a file discriminates nothing.
#
# A selector is the SUT's own DOM contract, and rule 9 REQUIRES E2E selection by `id`: generating one would
# break the rule that mandates it, so there is no end state to move to. A route is the endpoint under test.
# A JSON property name and a SQL parameter name are the wire contract an assertion exists to pin.
#
# EVERY ARM BLANKS ONE ARGUMENT POSITION, NEVER THE LINE. `FillAsync("#pricePaid", "999")` keeps the "999"
# and `CatalogStore.Create("LG OLED TV", price: 1299.99m)` is untouched, because those are data the SUT
# stores. The harness pins both directions for each arm: a line-level or area-level exemption here would be
# suppression wearing a position's name, and it would take the real rows with it.
$playwrightSelectorCall =
    'Locator|QuerySelectorAsync|QuerySelectorAllAsync|WaitForSelectorAsync|' +
    'ClickAsync|DblClickAsync|FillAsync|TypeAsync|PressAsync|CheckAsync|UncheckAsync|' +
    'HoverAsync|FocusAsync|TapAsync|SelectOptionAsync|SetInputFilesAsync|' +
    'IsVisibleAsync|IsHiddenAsync|IsEnabledAsync|IsDisabledAsync|IsCheckedAsync|IsEditableAsync|' +
    'InnerTextAsync|InnerHTMLAsync|TextContentAsync|InputValueAsync|GetAttributeAsync|' +
    'EvalOnSelectorAsync|EvalOnSelectorAllAsync|DispatchEventAsync|ScrollIntoViewIfNeededAsync'

# The route argument: Playwright navigation and the BCL HttpClient verbs. First position only, so a request
# BODY argument on the same line still sweeps.
#
# The literal must also BE route-shaped, because the bare BCL verb names are not owned by HttpClient.
# `(?<!\w)GetAsync` correctly excludes `StringGetAsync` and `HashGetAsync`, but it cannot exclude
# `cache.GetAsync("catalog-page-two")` on IDistributedCache, and a cache key is a specimen rather than an
# endpoint under test. Requiring a leading `/`, `~/` or a scheme keeps every real route in the fleet -
# they are all absolute paths - while handing the cache key back to rule 11. Narrowing by receiver NAME was
# the alternative and it is brittle: `client`, `_client`, `httpClient` and `factory.CreateClient()` are all
# the same receiver wearing four spellings, whereas the argument's shape is the thing the arm is about.
# `**/` is first because a Playwright route pattern is a GLOB, not a path: `RouteAsync("**/bff/user", ...)`
# registers a handler for a URL shape. Requiring a leading `/` alone silently un-exempted every route
# registration in an E2E fixture while leaving navigation exempt, which reads as the mock's own URLs being
# test data. A glob still cannot match a cache key, so the discriminator survives.
# An interpolation may also open with a hole that borrows a qualified member (`$"{AuthorizationNames.AdminFolder}/x"`)
# when a `/` or `?` follows it at once. Rule 11 prefers borrowing a production route constant to restating its
# value, and a route-shape test that admitted only a leading `/` forced the restatement. The member must be
# qualified and the separator must follow directly, so a local hole (`$"{section}/x"`) and a borrowed key with
# any other separator (`$"{CacheKeys.Prefix}:x"`) still report.
$routeLiteral = '[$@]{0,2}"(?:\*\*/|/|~/|\w+://|\{[A-Z]\w*(?:\.[A-Z]\w*)+\}[/?])(?:\\.|[^"\\])*"'
$routeArgumentCall =
    'GotoAsync|WaitForURLAsync|RouteAsync|UnrouteAsync|' +
    'GetAsync|GetStringAsync|GetByteArrayAsync|GetStreamAsync|GetFromJsonAsync|' +
    'PostAsync|PostAsJsonAsync|PutAsync|PutAsJsonAsync|PatchAsync|PatchAsJsonAsync|DeleteAsync|DeleteFromJsonAsync'

# The script argument of Playwright's evaluate family is JavaScript the test runs in the page: code, the same
# class as a selector, not a datum the SUT stores. First position only, so an ARGUMENT handed to the script
# (`EvaluateAsync(script, arg)`) still sweeps. Covers the inline literal, a literal wrapped onto the next line,
# a same-file const handed in (Get-BrowserScriptConstNames) and a raw or verbatim block in that position.
$browserScriptCall = 'EvaluateAsync|EvaluateHandleAsync|AddInitScriptAsync|WaitForFunctionAsync'
$browserScriptOpening = '(?:' + $browserScriptCall + ')\s*(?:<[^>]*>)?\s*\(\s*'

# A raw or verbatim block is browser code when it opens in the evaluate family's script position, on the
# call's line or alone on the line after it, or when it is the value of a const the file hands to that family.
function Test-BrowserScriptBlock([string[]]$lines, [int]$start) {
    $opening = $lines[$start - 1]
    $previous = if ($start -ge 2) { $lines[$start - 2] } else { '' }
    if ($opening -match ($browserScriptOpening + '[$@]*"')) { return $true }
    if ($opening -match '^\s*[$@]*"' -and $previous -match ($browserScriptOpening + '$')) { return $true }
    return $script:BrowserScriptConstNames -and
        $opening -match 'const\s+[\w.<>?\[\]]+\s+(\w+)\s*=\s*[$@]*"' -and
        $script:BrowserScriptConstNames.Contains($Matches[1])
}

function Get-BrowserScriptConstNames([string]$text) {
    $names = New-Object 'System.Collections.Generic.HashSet[string]' ([StringComparer]::Ordinal)
    foreach ($m in [regex]::Matches($text, $browserScriptOpening + '([A-Za-z_]\w*)\s*[,)]')) {
        [void]$names.Add($m.Groups[1].Value)
    }
    return $names
}

# Two kinds of named constant whose text is not test data, decided per file because the declaration and the
# use that proves it sit in different statements.
#
# SQL: a `const string` whose name ends in Sql AND whose value opens with a statement keyword. A SQL statement
# cannot be generated, so rule 11's "a value we own that genuinely cannot be generated earns a named
# constant" applies, and the declaration is that named constant. Both halves are required, so neither a
# rename nor a SELECT-shaped datum earns it alone; the harness pins each half's failure.
#
# Message: a `const string` local handed to Assert.True/False as the message argument in the same test. The
# inline message is already blanked; this is the same diagnostic one statement earlier. The map resets at
# every [Fact]/[Theory], and a constant compared as a value keeps reporting.
function Get-ContractTextLines([string[]]$lines) {
    $contractLines = New-Object 'System.Collections.Generic.HashSet[int]'
    $literal = '[$@]{0,2}"(?:\\.|[^"\\])*"'
    $literalOnly = "^$literal(?:\s*\+\s*$literal)*$"
    $messageCandidates = New-Object 'System.Collections.Generic.Dictionary[string,int[]]' ([StringComparer]::Ordinal)
    $statement = New-Object System.Text.StringBuilder
    $statementStart = 0

    for ($i = 0; $i -lt $lines.Count; $i++) {
        $code = (Remove-LineComment $lines[$i]).Trim()
        if ($code -match '^\[\s*(?:Fact|Theory)\b') {
            $messageCandidates.Clear()
            [void]$statement.Clear()
            continue
        }
        if ($code -eq '') { continue }
        if ($statement.Length -eq 0) { $statementStart = $i + 1 }
        [void]$statement.Append(' ').Append($code)
        if ($code -notmatch ';$') {
            if ($code -match '[{}]$') { [void]$statement.Clear() }
            continue
        }

        $text = $statement.ToString().Trim()
        [void]$statement.Clear()
        $span = [int[]]($statementStart..($i + 1))

        if ($text -match '^(?:(?:private|internal|public|protected|static)\s+)*const\s+string\s+(\w+)\s*=\s*(.+?)\s*;$') {
            $name = $Matches[1]
            $value = $Matches[2]
            if ($value -notmatch $literalOnly) { continue }
            if ($name -cmatch 'Sql$' -and $value -cmatch $sqlStatementOpening) {
                foreach ($n in $span) { [void]$contractLines.Add($n) }
            }
            else {
                $messageCandidates[$name] = $span
            }
            continue
        }

        # The same named SQL statement, declared as a local because it interpolates a generated name, which
        # `const` forbids. Both halves of the const arm still decide it - the name ends in Sql AND the value
        # opens with a statement keyword - so a differently named local holding the same text, and a *Sql
        # local holding anything else, both keep reporting.
        # Bind both groups BEFORE the next comparison: `-cmatch` repopulates $Matches, so testing
        # `$Matches[2]` after matching the name against 'Sql$' reads a match that has no groups, and the
        # arm silently never fires. The harness caught exactly that.
        if ($text -match '^(?:var|string)\s+(\w+)\s*=\s*(.+?)\s*;$') {
            $localName = $Matches[1]
            $localValue = $Matches[2]
            if ($localName -cmatch 'Sql$' -and $localValue -cmatch $sqlStatementOpening) {
                foreach ($n in $span) { [void]$contractLines.Add($n) }
                continue
            }
        }

        if ($text -match '^Assert\.(?:True|False)\s*\(.+,\s*(\w+)\s*(?:\+[^,]*)?\)\s*;$' -and
            $messageCandidates.ContainsKey($Matches[1])) {
            foreach ($n in $messageCandidates[$Matches[1]]) { [void]$contractLines.Add($n) }
        }
    }

    return , $contractLines
}

# Same-file helpers that take SQL text. A helper qualifies when it has exactly one string parameter (so the
# literal a caller passes is unambiguously that parameter) and the parameter either reaches a *sql or
# *CommandText receiver's search method in the helper's body, is forwarded to a helper that already
# qualifies, or is itself named sql/*Sql - the same name anchor the receiver arms use. A helper whose string
# reaches any other receiver does not qualify, which is the harness control.
function Get-SqlHelperNames([string[]]$lines) {
    $helpers = @{}
    $declaration = '^\s*(?:(?:private|internal|public|protected|static|async)\s+)+[\w<>?\[\],\s]+?\s+(\w+)\s*\(([^)]*)\)\s*(=>|\{|$)'
    for ($i = 0; $i -lt $lines.Count; $i++) {
        $code = Remove-LineComment $lines[$i]
        if ($code -notmatch $declaration) { continue }
        $name = $Matches[1]
        $parameterList = $Matches[2]
        $stringParameters = [regex]::Matches($parameterList, '\bstring\??\s+(\w+)')
        if ($stringParameters.Count -ne 1) { continue }

        $body = New-Object System.Text.StringBuilder
        [void]$body.Append($code.Substring($code.IndexOf(')') + 1))
        $depth = ([regex]::Matches($code, '\{')).Count - ([regex]::Matches($code, '\}')).Count
        $isExpression = $code -match '=>'
        $j = $i
        while (($isExpression -and $body.ToString() -notmatch ';') -or (-not $isExpression -and ($depth -gt 0 -or $j -eq $i))) {
            $j++
            if ($j -ge $lines.Count) { break }
            $next = Remove-LineComment $lines[$j]
            [void]$body.Append("`n").Append($next)
            if (-not $isExpression) {
                if ($j -eq $i + 1 -and $code -notmatch '\{' -and $next -match '^\s*=>') { $isExpression = $true; continue }
                $depth += ([regex]::Matches($next, '\{')).Count - ([regex]::Matches($next, '\}')).Count
            }
        }
        $helpers[$name] = @{ Parameter = $stringParameters[0].Groups[1].Value; Body = $body.ToString() }
    }

    $sqlHelpers = New-Object 'System.Collections.Generic.HashSet[string]' ([StringComparer]::Ordinal)
    $changed = $true
    while ($changed) {
        $changed = $false
        foreach ($name in @($helpers.Keys)) {
            if ($sqlHelpers.Contains($name)) { continue }
            $parameter = [regex]::Escape($helpers[$name].Parameter)
            $body = $helpers[$name].Body
            $qualifies = $helpers[$name].Parameter -cmatch '^(?:sql|\w+Sql)$' -or
                $body -match "(?:[Ss]ql|CommandText)\s*\??\.\s*(?:Contains|StartsWith|EndsWith|IndexOf)\s*\(\s*(?:[`$@]{0,2}""[^""]*\{\s*$parameter\s*\}[^""]*""|$parameter\b)"
            if (-not $qualifies) {
                foreach ($other in $sqlHelpers) {
                    if ($body -match "(?<![.\w])$([regex]::Escape($other))\s*\((?:[^()]|\([^()]*\))*\b$parameter\b") { $qualifies = $true; break }
                }
            }
            if ($qualifies) {
                [void]$sqlHelpers.Add($name)
                $changed = $true
            }
        }
    }

    return , $sqlHelpers
}

# Same-file helpers that take a JSON PROPERTY NAME. A wire property name is the contract an assertion or a
# mock exists to pin, exactly as a SQL parameter name is, and the direct `GetProperty("x")` form is already
# exempt. A hand-rolled reader hides the same position behind one call: `ReadString(root, "name")` whose
# body is `root.TryGetProperty(name, out var v)`. Without this the mock server in an E2E fixture reports
# every property of every payload it reads, and "generating" one would stop the mock answering the request.
#
# A helper qualifies only when its string parameter reaches GetProperty/TryGetProperty in its own body, or
# is forwarded to a helper that already qualifies. A helper whose string reaches anything else does not,
# which is the harness control: the exemption follows the evidence rather than the name.
function Get-JsonPropertyHelperNames([string[]]$lines) {
    $helpers = @{}
    $declaration = '^\s*(?:(?:private|internal|public|protected|static|async)\s+)+[\w<>?\[\],\s]+?\s+(\w+)\s*\(([^)]*)\)\s*(=>|\{|$)'
    for ($i = 0; $i -lt $lines.Count; $i++) {
        $code = Remove-LineComment $lines[$i]
        if ($code -notmatch $declaration) { continue }
        $name = $Matches[1]
        $parameterList = $Matches[2]
        $stringParameters = [regex]::Matches($parameterList, '\bstring\??\s+(\w+)')
        if ($stringParameters.Count -ne 1) { continue }

        $body = New-Object System.Text.StringBuilder
        [void]$body.Append($code.Substring($code.IndexOf(')') + 1))
        $depth = ([regex]::Matches($code, '\{')).Count - ([regex]::Matches($code, '\}')).Count
        $isExpression = $code -match '=>'
        $j = $i
        while (($isExpression -and $body.ToString() -notmatch ';') -or (-not $isExpression -and ($depth -gt 0 -or $j -eq $i))) {
            $j++
            if ($j -ge $lines.Count) { break }
            $next = Remove-LineComment $lines[$j]
            [void]$body.Append("`n").Append($next)
            if (-not $isExpression) {
                if ($j -eq $i + 1 -and $code -notmatch '\{' -and $next -match '^\s*=>') { $isExpression = $true; continue }
                $depth += ([regex]::Matches($next, '\{')).Count - ([regex]::Matches($next, '\}')).Count
            }
        }
        $helpers[$name] = @{ Parameter = $stringParameters[0].Groups[1].Value; Body = $body.ToString() }
    }

    $jsonHelpers = New-Object 'System.Collections.Generic.HashSet[string]' ([StringComparer]::Ordinal)
    $changed = $true
    while ($changed) {
        $changed = $false
        foreach ($name in @($helpers.Keys)) {
            if ($jsonHelpers.Contains($name)) { continue }
            $parameter = [regex]::Escape($helpers[$name].Parameter)
            $body = $helpers[$name].Body
            $qualifies = $body -match "(?:Try)?GetProperty\s*\(\s*$parameter\b"
            if (-not $qualifies) {
                foreach ($other in $jsonHelpers) {
                    if ($body -match "(?<![.\w])$([regex]::Escape($other))\s*\((?:[^()]|\([^()]*\))*\b$parameter\b") { $qualifies = $true; break }
                }
            }
            if ($qualifies) {
                [void]$jsonHelpers.Add($name)
                $changed = $true
            }
        }
    }

    return , $jsonHelpers
}

# The same evidence for a string that never becomes a property: a parameter, field or explicitly typed
# local that this file passes, bare, to a conversion. `LibraryRepository.AddAsync(string identitySub)`
# followed by `Guid.Parse(identitySub)` is a Guid the signature refuses to say. Decided per file because
# the declaration and the conversion sit on different lines; a name converted in one file lends nothing to
# another.
function Test-LambdaBoundAt([string]$code, [int]$index, [string]$name) {
    # A lambda parameter is that lambda's own variable, so converting it is no evidence about a string of
    # the same name declared elsewhere in the file. A '(' straight after an identifier opens a method's
    # parameter list, not a lambda's, which is what keeps `RecordOwner(string ownerSub) =>` counted.
    $start = $code.LastIndexOfAny([char[]]@(';', '{', '}'), [Math]::Max(0, $index - 1)) + 1
    $statement = $code.Substring($start, $index - $start)
    $bound = [regex]::Escape($name)
    return ($statement -match "(?<![\w.])$bound\s*=>") -or
        ($statement -match "(?<![\w>\]])\(\s*(?:[^()]*,\s*)?(?:[\w.<>\[\]?]+\s+)?$bound\s*(?:,[^()]*)?\)\s*=>")
}

function Get-ParsedStringLocals([string]$text) {
    $parsed = New-Object 'System.Collections.Generic.Dictionary[string,string]' ([StringComparer]::Ordinal)
    $bare = '(?<name>[a-z_]\w*)\b(?!\s*[.(\[?!])'
    $conversions = @(
        "\b(?<type>Guid|DateTimeOffset|DateTime|DateOnly|TimeOnly|TimeSpan|int|long|short|decimal|double|float|bool|Int32|Int64|Decimal|Double|Boolean)\.Parse(?:Exact)?\s*\(\s*$bare"
        "\bnew\s+(?<type>Uri)\s*\(\s*$bare"
        "\bEnum\.Parse\s*<\s*(?<type>\w+)\s*>\s*\(\s*$bare"
        "\bConvert\.To(?<type>Int32|Int64|Decimal|Double|DateTime|Boolean)\s*\(\s*$bare"
    )
    $code = [regex]::Replace($text, '(?m)^\s*//.*$', '')
    foreach ($pattern in $conversions) {
        foreach ($m in [regex]::Matches($code, $pattern)) {
            $name = $m.Groups['name'].Value
            if (Test-LambdaBoundAt $code $m.Index $name) { continue }
            if (-not $parsed.ContainsKey($name)) { $parsed[$name] = $m.Groups['type'].Value }
        }
    }
    return , $parsed
}

$constantsFilePattern = 'Constants\.cs$'
$script:ConstantsFileCache = @{}

function Test-ConstantsOnlyFile([string]$fullPath) {
    if ($script:ConstantsFileCache.ContainsKey($fullPath)) { return $script:ConstantsFileCache[$fullPath] }
    $verdict = $false
    $text = Get-Content -LiteralPath $fullPath -Raw -ErrorAction SilentlyContinue
    if ($text) {
        # Strip comments so a commented-out method cannot disqualify a genuine constants file.
        $body = [regex]::Replace($text, '//[^\r\n]*', '')
        $body = [regex]::Replace($body, '/\*[\s\S]*?\*/', '')
        $isStaticClass = $body -match '\bstatic\s+(?:partial\s+)?class\b'
        # Any member that is not a constant: a method or local function (parentheses before a brace or
        # an arrow), a property (braces with get/set), or an event.
        $hasNonConstantMember = $body -match '\)\s*(?:=>|\{)' -or $body -match '\{\s*get\b' -or $body -match '\bevent\b'
        $verdict = $isStaticClass -and -not $hasNonConstantMember
    }
    $script:ConstantsFileCache[$fullPath] = $verdict
    return $verdict
}

# The TypeScript spelling of the same owner: a `*-constants.ts` (or `*.constants.ts`) module whose every
# declaration is an `export const` of literal data. A function, an arrow, a class, a call, a template
# literal or a `let` disqualifies it, exactly as a method disqualifies a C# class; `export type` aliases
# are type-level and are dropped before the test, since `(typeof X)[keyof typeof X]` carries parentheses.
$tsConstantsFilePattern = '(?i)(?:^|[\\/.-])constants\.(?:tsx?|mts|cts)$'
$typeScriptExtensions = @('.ts', '.tsx', '.mts', '.cts')

function Test-TsConstantsOnlyFile([string]$fullPath) {
    if ($script:ConstantsFileCache.ContainsKey($fullPath)) { return $script:ConstantsFileCache[$fullPath] }
    $verdict = $false
    $text = Get-Content -LiteralPath $fullPath -Raw -ErrorAction SilentlyContinue
    if ($text) {
        $body = [regex]::Replace($text, '/\*[\s\S]*?\*/', '')
        $body = [regex]::Replace($body, '(?m)//[^\r\n]*$', '')
        $body = [regex]::Replace($body, "'(?:\\.|[^'\\\r\n])*'|""(?:\\.|[^""\\\r\n])*""", "''")
        $body = [regex]::Replace($body, '(?ms)^\s*export\s+type\b.*?;', '')
        $body = [regex]::Replace($body, '(?m)^\s*import\b[^;]*;', '')
        $hasExportConst = $body -match '(?m)^\s*export\s+const\s+\w+'
        $hasNonConstant = $body -match '=>' -or $body -match '\bfunction\b' -or $body -match '\bclass\b' -or
            $body -match '\(' -or $body -match '`' -or $body -match '(?m)^\s*(?:export\s+)?(?:let|var)\b'
        $verdict = $hasExportConst -and -not $hasNonConstant
    }
    $script:ConstantsFileCache[$fullPath] = $verdict
    return $verdict
}

# The Python spelling: a test-owned `*_constants.py` module whose every top-level line is an UPPER_CASE
# assignment of literal data (or the bracket that closes one). An independent oracle a test compares production
# against, such as a security allow-list or an expected schema inventory, lives here: derived from production,
# it would pass by construction.
$pyConstantsFilePattern = '(?i)(?:^|[\\/])\w*_constants\.py$'

function Test-PyConstantsOnlyFile([string]$fullPath) {
    if ($script:ConstantsFileCache.ContainsKey($fullPath)) { return $script:ConstantsFileCache[$fullPath] }
    $verdict = $false
    $text = Get-Content -LiteralPath $fullPath -Raw -ErrorAction SilentlyContinue
    if ($text) {
        $body = [regex]::Replace($text, '"""[\s\S]*?"""|''''''[\s\S]*?''''''', "''")
        $body = [regex]::Replace($body, "'(?:\\.|[^'\\\r\n])*'|""(?:\\.|[^""\\\r\n])*""", "''")
        $body = [regex]::Replace($body, '(?m)#[^\r\n]*$', '')
        $body = [regex]::Replace($body, '(?m)^(?:from|import)\s[^\r\n]*$', '')
        $topLevel = @($body -split "`r?`n" | Where-Object { $_ -match '^\S' })
        $hasAssignment = @($topLevel | Where-Object { $_ -cmatch '^[A-Z_][A-Z0-9_]*\s*(?::[^=]+)?=' }).Count -gt 0
        $strayTopLevel = @($topLevel | Where-Object { $_ -cnotmatch '^(?:[A-Z_][A-Z0-9_]*\s*(?::[^=]+)?=|[)\]}])' }).Count -gt 0
        $hasNonConstant = $body -match '\b(?:def|class|lambda)\b' -or $body -match '\w\s*\('
        $verdict = $hasAssignment -and -not $strayTopLevel -and -not $hasNonConstant
    }
    $script:ConstantsFileCache[$fullPath] = $verdict
    return $verdict
}

function Test-OwnerConstantsFile([System.IO.FileInfo]$file, [string]$relPath) {
    switch -Regex ($file.Extension) {
        '^\.cs$' { return $relPath -match $constantsFilePattern -and (Test-ConstantsOnlyFile $file.FullName) }
        '^\.(?:tsx?|mts|cts)$' { return $relPath -match $tsConstantsFilePattern -and (Test-TsConstantsOnlyFile $file.FullName) }
        '^\.py$' { return $relPath -match $pyConstantsFilePattern -and (Test-PyConstantsOnlyFile $file.FullName) }
        default { return $false }
    }
}

# The generator heads the right-hand side outright, with optional conversion chain
# (`.ToString()`, `.ToLowerInvariant()`).
# Arithmetic after the generator is a SCALE, not a second value: `Random.Shared.Next(0, 10) / 2.0` puts the
# draw on the 0-5 half-step scale the SUT uses, and the local still names the generated value. The chain
# previously allowed only `.Method()` calls, so every scaled draw was charged for complying. A trailing `,`
# is allowed because the same shape appears as an object-initializer member, which names it just as a local
# does. The generator must still HEAD the right-hand side; the moment it is an argument to something else
# the local names that something else instead, and the row stands.
$generatorHeadsRhsPattern = '^(?:Guid\.NewGuid\(\)|Random\.Shared\.Next(?:Double)?\([^)]*\))(?:\s*\.\w+\([^)]*\))*(?:\s*[-+*/%]\s*[\w.]+)*\s*[;,]?\s*$'

function Test-GeneratedValueLine($repo, $relPath, $lineNum, $area, $raw) {
    if ($script:RawStringInside -and $script:RawStringInside.Contains($lineNum)) { return }

    $code = Remove-LineComment $raw
    if ($code.Trim() -eq '') { return }

    $generators = [regex]::Matches($code, $generatorCallPattern)
    if ($generators.Count -eq 0) { return }
    if ($code -match $generatorMethodPattern) { return }

    if ($code -match $localDeclarationPattern) {
        $name = $Matches[1]
        $rhs = $code.Substring($Matches[0].Length)

        # Two ways the generator IS the value being bound, both of which satisfy rule 11:
        #   var churchId      = Guid.NewGuid();                  <- heads the RHS
        #   var configuredKey = $"value{Guid.NewGuid():N}";      <- interpolated into the bound string
        # The second form used to be reported as an inline violation, which charged the author for
        # complying - the value is already in a well-named local.
        $headsRhs = $rhs.TrimStart() -match $generatorHeadsRhsPattern
        $inInterpolation = $false
        foreach ($s in [regex]::Matches($rhs, '\$@?"(?:[^"\\]|\\.)*"')) {
            if ($s.Value -match $generatorCallPattern) { $inInterpolation = $true }
        }

        if ($headsRhs -or $inInterpolation) {
            if ($name -match $weakLocalNamePattern) {
                Add-Row $repo $relPath $lineNum $area 11 "undescriptive-local-$name" 'rename-local' $raw
            }
            # A bound generator can still sit beside a second, unbound one on the same line.
            if ($generators.Count -gt 1 -and -not $inInterpolation) {
                Add-Row $repo $relPath $lineNum $area 11 'inline-generated-value' 'extract-to-named-local' $raw
            }
            return
        }

        # A declaration whose generator is an argument to something else - `var result =
        # await service.DeleteAsync(Guid.NewGuid(), ct)`. The local names the result, not the id, so the
        # generated value is still unnamed and unassertable.
        Add-Row $repo $relPath $lineNum $area 11 'inline-generated-value' 'extract-to-named-local' $raw
        return
    }

    Add-Row $repo $relPath $lineNum $area 11 'inline-generated-value' 'extract-to-named-local' $raw
}

# C# raw string literals (`"""`) span lines, and this scanner is line-based, so a line inside one reads as
# bare code. That is actively destructive for rule 11: a line reading `VALUES ($1, $2, $3, $4)` inside a
# raw SQL block carries no quotes for Remove-LiteralBodies to strip, so 2/3/4 surface as magic numbers and
# "generating" them would destroy the statement. (`1` escaped only because keep-boundary skips 0/1 - luck,
# not design.) Map the blocks once per file; each becomes ONE needs-triage row at its opening fence rather
# than a flood of per-literal rows, because the block is what a human has to judge.
# A verbatim string closes at the first `"` that is not doubled; `""` is an escaped quote inside the body.
# Scanning from $from lets the opening `@"` be skipped on the line that opens the block.
function Test-VerbatimStringCloses([string]$text, [int]$from) {
    for ($i = $from; $i -lt $text.Length; $i++) {
        if ($text[$i] -ne '"') { continue }
        if ($i + 1 -lt $text.Length -and $text[$i + 1] -eq '"') { $i++; continue }
        return $true
    }
    return $false
}

# `@"` opens a verbatim block only when the match is not itself inside an ordinary literal. `var atSign =
# "@";` is quote, at, quote: a naive match fires on the `@"` at index 1, finds no further quote after it,
# and opens a block that swallows every remaining line of the file - one bogus needs-triage row at the
# phantom opening and rule 11 silenced for the rest of the file. Counting the unescaped quotes before the
# match settles it, because an odd count means the match sits inside a literal. Every match is tried rather
# than only the first, so a real opener later on the line is still found: `Foo("a@", @"...` has a phantom
# match at the `a@"` and a genuine one after it.
function Get-VerbatimOpenMatch([string]$text) {
    foreach ($match in [regex]::Matches($text, '\$?@"|@\$"')) {
        $quotes = 0
        for ($i = 0; $i -lt $match.Index; $i++) {
            if ($text[$i] -eq '\') { $i++; continue }
            if ($text[$i] -eq '"') { $quotes++ }
        }

        if ($quotes % 2 -eq 0) { return $match }
    }

    return $null
}

function Get-RawStringMap($lines) {
    $inside = New-Object 'System.Collections.Generic.HashSet[int]'
    $starts = New-Object 'System.Collections.Generic.List[int]'
    $verbatimStarts = New-Object 'System.Collections.Generic.List[int]'
    $inRaw = $false
    $inVerbatim = $false
    for ($i = 0; $i -lt $lines.Count; $i++) {
        # A multi-line `@"..."` is the same hazard as `"""`, and it is how this fleet embeds JavaScript in
        # C#: PasskeyCeremonyTests held `const button = document.querySelector(...)` inside one, which the
        # line scanner read as a C# `const` and reported as a rule-11 row against JavaScript.
        if ($inVerbatim) {
            [void]$inside.Add($i + 1)
            if (Test-VerbatimStringCloses $lines[$i] 0) { $inVerbatim = $false }
            continue
        }
        $fences = ([regex]::Matches($lines[$i], '"{3,}')).Count
        if ($fences -eq 0 -and -not $inRaw) {
            $open = Get-VerbatimOpenMatch $lines[$i]
            if ($null -ne $open -and -not (Test-VerbatimStringCloses $lines[$i] ($open.Index + $open.Length))) {
                $inVerbatim = $true
                [void]$verbatimStarts.Add($i + 1)
                [void]$inside.Add($i + 1)
            }
            continue
        }
        if (-not $inRaw) {
            # An odd count OPENS a multi-line block; an even non-zero count is a raw string that begins and
            # ends on this line. Both are payloads and both are judged as a unit, so both earn one row -
            # but only the block goes into $inside, because a self-contained one shares its line with
            # ordinary code whose literals must still sweep. Its own body is blanked in
            # Scan-TestLiteralLine instead.
            #
            # Before this, a self-contained raw string was left entirely to the ordinary `"..."` regex,
            # which cannot parse `"""{"id": 201930}"""`: it matched junk fragments out of the middle, and
            # the numeric sweep read the vendor's own ids as magic numbers. Those rows were not findings,
            # they were the scanner mis-reading a payload - and CODE-STYLE.md already says a raw block is
            # judged as a unit, which is just as true when it fits on one line.
            if ($fences % 2 -eq 1) {
                $inRaw = $true
                [void]$starts.Add($i + 1)
                [void]$inside.Add($i + 1)
            }
            elseif ($fences -gt 0) {
                [void]$starts.Add($i + 1)
            }
        }
        else {
            [void]$inside.Add($i + 1)
            if ($fences % 2 -eq 1) { $inRaw = $false }
        }
    }
    return @{ Inside = $inside; Starts = $starts; VerbatimStarts = $verbatimStarts }
}

# True when the statement's earlier physical lines have already entered the MESSAGE argument of an
# Assert.True/False/SkipUnless/SkipWhen - the diagnostic the single-line arm at $withoutNameof already
# blanks. The discriminator is the comma count at paren depth one, never the mere presence of the call:
# a literal wrapped inside the CONDITION is reached with the same prefix and is real test data.
# Both are counted off the prefix, whose literal bodies are already removed, so a parenthesis or comma
# inside a string cannot move the depth.
function Test-InAssertMessageArgument([string]$statementPrefix) {
    if ($statementPrefix -eq '') { return $false }
    $call = [regex]::Matches($statementPrefix, 'Assert\.(?:True|False|SkipUnless|SkipWhen)\s*\(')
    if ($call.Count -eq 0) { return $false }
    $last = $call[$call.Count - 1]
    $depth = 1
    $commas = 0
    for ($i = $last.Index + $last.Length; $i -lt $statementPrefix.Length; $i++) {
        $ch = $statementPrefix[$i]
        if ($ch -eq '(') { $depth++ }
        elseif ($ch -eq ')') {
            $depth--
            # The assertion closed before this line, so whatever follows belongs to a later statement.
            if ($depth -le 0) { return $false }
        }
        elseif ($ch -eq ',' -and $depth -eq 1) { $commas++ }
    }
    return $commas -ge 1
}

# A framework attribute wrapped across lines: its opening line is exempt through $frameworkAttributePattern,
# and its continuation lines are the same argument list. "Still open" is the test, not "the prefix starts
# with one", because the prefix keeps the attribute's lines until a `;`, `{` or `}` and so still carries
# them on the member signature below, whose literals are ordinary.
function Test-InsideFrameworkAttribute([string]$statementPrefix) {
    $prefixLines = @($statementPrefix -split "`n")
    for ($k = $prefixLines.Count - 1; $k -ge 0; $k--) {
        if ($prefixLines[$k] -match $frameworkAttributePattern) {
            $opened = $prefixLines[$k..($prefixLines.Count - 1)] -join "`n"
            return ([regex]::Matches($opened, '\[').Count -gt [regex]::Matches($opened, '\]').Count)
        }
    }
    return $false
}

function Scan-TestLiteralLine($repo, $relPath, $lineNum, $area, $raw, $statementPrefix = '', $continuation = '') {
    if ($script:RawStringInside -and $script:RawStringInside.Contains($lineNum)) { return }
    # A pin test in its rule-18 shape. See Get-PinnedLiteralLines.
    if ($script:PinnedLiteralLines -and $script:PinnedLiteralLines.Contains($lineNum)) { return }
    # A SQL constant or an assertion-message constant. See Get-ContractTextLines.
    if ($script:ContractTextLines -and $script:ContractTextLines.Contains($lineNum)) { return }
    # A dedicated constants class is the end state for a named constant, provided the file earns the name.
    if ($relPath -match $constantsFilePattern -and $script:CurrentFileIsConstantsOnly) { return }
    # A `const` an attribute in this same file names is compelled by the language: an attribute argument
    # cannot be anything but a constant expression. See Get-AttributeReferencedNames.
    if ($script:AttributeReferencedNames -and
        $raw -match '^\s*(?:(?:private|internal|public|protected|static|readonly)\s+)*const\s+[\w.<>?\[\]]+\s+(\w+)\s*=' -and
        $script:AttributeReferencedNames.Contains($Matches[1])) {
        return
    }
    # A `const` this same file uses as a DEFAULT PARAMETER VALUE is compelled by the language, exactly as
    # an attribute argument is. See Get-DefaultParameterNames.
    if ($script:DefaultParameterNames -and
        $raw -match '^\s*(?:(?:private|internal|public|protected|static|readonly)\s+)*const\s+[\w.<>?\[\]]+\s+(\w+)\s*=' -and
        $script:DefaultParameterNames.Contains($Matches[1])) {
        return
    }
    # A `const` this same file hands to EnvironmentSetting as a default or a bound is the config schema's
    # value, not test data. See Get-ConfigSchemaConstNames.
    if ($script:ConfigSchemaConstNames -and
        $raw -match '^\s*(?:(?:private|internal|public|protected|static|readonly)\s+)*const\s+[\w.<>?\[\]]+\s+(\w+)\s*=' -and
        $script:ConfigSchemaConstNames.Contains($Matches[1])) {
        return
    }
    # A `const` this same file hands to the evaluate family as its script is browser code. The value may sit on
    # the declaration's own line or wrap onto the next. See Get-BrowserScriptConstNames.
    if ($script:BrowserScriptConstNames -and $script:BrowserScriptConstNames.Count -gt 0) {
        $constDeclaration = '(?:(?:private|internal|public|protected|static|readonly)\s+)*const\s+[\w.<>?\[\]]+\s+(\w+)\s*='
        if ($raw -match ('^\s*' + $constDeclaration) -and $script:BrowserScriptConstNames.Contains($Matches[1])) {
            return
        }
        if ($statementPrefix -match ($constDeclaration + '\s*$') -and
            $script:BrowserScriptConstNames.Contains($Matches[1])) {
            return
        }
    }

    $code = Remove-LineComment $raw
    if ($code.Trim() -eq '') { return }
    # A raw string opening and closing on this line is a payload, already reported once by Get-RawStringMap
    # and judged as a unit. Blank its body before anything else reads the line: the ordinary `"..."` regex
    # cannot parse `"""{"id": 201930}"""` and pulls fragments out of its middle, and the numeric sweep
    # reads the vendor's ids as magic numbers. Only the raw string is blanked, so an ordinary literal
    # sharing the line with it still reports - which is what the harness control pins.
    $code = [regex]::Replace($code, '"{3,}.*?"{3,}', 'R')
    if ($code -match '^\s*namespace\b') { return }
    # `using` opens two unrelated constructs and only ONE of them is out of rule 11's scope. A using
    # DIRECTIVE (`using System.Net;`, `using static X;`, `using Alias = Some.Type;`) imports a name and
    # carries no test data. A using DECLARATION (`using var cts = new CancellationTokenSource(
    # TimeSpan.FromMilliseconds(300));`) is an ordinary local with an ordinary literal in it, and skipping
    # it made every disposable local in the fleet invisible to this rule - a FALSE NEGATIVE, which is the
    # worse direction. The discriminator is two tokens before the `=`: a declaration names a type (or
    # `var`) AND an identifier, an alias directive names only the alias.
    $isUsingDeclaration = $code -match '^\s*using\s+(?:var\b|[\w.<>\[\],?]+\s+\w+\s*=)'
    if ($code -match '^\s*(?:global\s+)?using\b' -and -not $isUsingDeclaration) { return }
    if ($code -match $frameworkAttributePattern) { return }
    if (Test-InsideFrameworkAttribute $statementPrefix) { return }

    $isAttribute = $code -match '^\s*\['
    # A collection expression of literals whose Assert.Equal partner is a production member is an
    # EXTERNAL-SPEC PIN, and it is the one rule 11 shape with no code fix available: substituting the
    # production constants turns the assertion into Assert.Equal(X, X), a check that cannot fail. The
    # single-literal form of this pin is already blanked further down; this is the same pin wrapped across
    # lines, which is how a seven-element expected array is actually formatted. Anchored on the partner
    # resolving to a qualified, capitalised member, so a local on the actual side keeps reporting - that
    # control is what stops this from becoming "any collection expression is exempt".
    if ($statementPrefix -match 'Assert\.Equal\s*(?:<[^>]*>)?\s*\(\s*$' -and
        $code -match '^\s*\[.*\]\s*,\s*$' -and
        $continuation -match '^\s*[A-Z]\w*(?:\.\w+)+\s*\)\s*;') {
        return
    }

    $withoutNameof = [regex]::Replace($code, 'nameof\s*\([^)]*\)', 'N')
    # Two string arguments that are labels rather than test data, blanked before the literal sweep: the
    # message of a `new <Anything>Exception(...)` (nothing asserts on it; generating it only makes a failure
    # unreadable) and the name argument of `DataTable.Columns.Add(...)` (every fleet reader takes the
    # ordinal, so the label decides nothing). The literal sweep on the rest of the line is unchanged.
    # An exception message one of whose holes holds a literal of its own
    # (`$"got {value?.GetType().Name ?? "null"} from: {sql}"`). This runs BEFORE the plain-message sibling
    # below, which stops at the nested quote and would otherwise leave the message's own words reporting as
    # test data. The message is still the anchor: a hole carrying a literal is consumed only inside a
    # `new *Exception(` argument.
    $withoutNameof = [regex]::Replace(
        $withoutNameof,
        '(new\s+\w*Exception\s*\(\s*)\$@?"(?:[^"{}\\]|\\.|\{(?:[^{}"]|"(?:\\.|[^"\\])*")*\})*"',
        '$1N')
    $withoutNameof = [regex]::Replace($withoutNameof, '(new\s+\w*Exception\s*\(\s*)[$@]{0,2}"(?:\\.|[^"\\])*"', '$1N')
    $withoutNameof = [regex]::Replace($withoutNameof, '(Columns\.Add\s*\(\s*)"(?:\\.|[^"\\])*"', '$1N')
    # More labels and contract text, blanked the same way: the message argument of Assert.True/False (a
    # diagnostic nothing asserts on), a SQL parameter name read back through the Parameters indexer, SQL
    # text asserted against a receiver named *sql/*CommandText (the query text is the thing under test, in
    # either dialect), a ToString format specifier, and a literal pinned by Assert.Equal against a
    # production constant (rule 11: a pin test asserts the literal; substituting the constant is a
    # tautology). A literal compared to a lower-case result still sweeps.
    # Assert.SkipUnless/SkipWhen carry the same trailing diagnostic argument as Assert.True/False - xUnit v3's
    # skip reason, which nothing asserts on and generating only makes unreadable.
    $withoutNameof = [regex]::Replace($withoutNameof, '(Assert\.(?:True|False|SkipUnless|SkipWhen)\s*\(.*,\s*)[$@]{0,2}"(?:\\.|[^"\\])*"(?=\s*\)\s*;?\s*$)', '$1N')
    # An HTTP HEADER NAME is a wire contract fixed outside this fleet (Sony's x-psn-*, Apollo's
    # apollo-require-preflight, the RFC names), the same position as the SQL parameter name and the
    # DataTable column label above. First argument only, so the header VALUE still reports - which is the
    # control, because a value is data the test chose.
    $withoutNameof = [regex]::Replace(
        $withoutNameof,
        '(Headers\.(?:Add|TryAddWithoutValidation|Remove|Contains)\s*\(\s*)[$@]{0,2}"(?:\\.|[^"\\])*"',
        '$1N')
    $withoutNameof = [regex]::Replace($withoutNameof, '(Parameters\s*\[\s*)"@[^"]*"', '$1N')
    # The PATTERN argument of Assert.Matches/DoesNotMatch is the property being asserted, not test data.
    # Rule 11 asks a test about a property to construct its inputs from generated values and assert the
    # property; a regex is how that property is written down, so generating it would destroy the
    # assertion. `Assert.Matches("[A-Z]", password)` says the generator produces an upper-case character,
    # and there is no generated value that could stand in for `[A-Z]`.
    # First argument only, so a literal in the ACTUAL position still reports - that is the control, and
    # it is the shape that means the test pinned a specimen instead of generating one.
    $withoutNameof = [regex]::Replace(
        $withoutNameof,
        '(Assert\.(?:Matches|DoesNotMatch)\s*\(\s*)[$@]{0,2}"(?:\\.|[^"\\])*"',
        '$1N')
    # The same SQL parameter name, asked as a membership question rather than read back through the
    # indexer: `Assert.True(cmd.Parameters.Contains("@DayOfWeek"))` pins that the builder bound that
    # parameter, which is the SQL contract the test exists to check. Anchored on the `@` prefix AND the
    # Parameters receiver, so an ordinary string argument to any other Contains still reports.
    $withoutNameof = [regex]::Replace($withoutNameof, '(Parameters\.Contains\s*\(\s*)"@[^"]*"', '$1N')
    # The receiver may reach the statement through an indexer, which is how a captured-command list is read
    # in practice: connection.ExecutedCommands[2].CommandText. A lookahead of [\w.]* alone stops at the '[',
    # so every such assertion reported; the (?:\[...\][\w.]*)* segment lets the path cross indexers. The
    # anchor is still the *sql/*CommandText suffix, so a magic string on any other receiver keeps firing.
    # The `[$@]{0,2}` prefix matches the sibling blanking above (Assert.True/False messages), which has
    # always accepted it. Without it the expected value's SYNTAX
    # decided the verdict rather than its receiver: `Assert.Equal("...sql...", cmd.CommandText)` was
    # contract text while the interpolated `Assert.Equal($"...{x}...", cmd.CommandText)` was a magic
    # string, though both assert the same statement. The non-SQL receiver control in the harness keeps
    # this honest - interpolation is not the license, the receiver is.
    # The path also crosses a call (`Assert.Single(commands).ExecutedSql`) and a null-conditional access
    # (`CapturedCommandText?.Contains`), and IndexOf/LastIndexOf/Split on the receiver locate the same text.
    # Each is the same receiver reached another way, and the suffix anchor is unchanged: the harness keeps a
    # non-SQL receiver control beside every one of them.
    $withoutNameof = [regex]::Replace($withoutNameof, "(Assert\.(?:Contains|DoesNotContain|StartsWith|EndsWith|Equal)\s*\()[`$@]{0,2}`"(?:\\.|[^`"\\])*`"(?=\s*,\s*$sqlReceiverPath)", '$1N')
    $withoutNameof = [regex]::Replace($withoutNameof, "($sqlReceiverPath\s*\??\.\s*(?:Contains|StartsWith|EndsWith|IndexOf|LastIndexOf|Split)\s*\(\s*)[`$@]{0,2}`"(?:\\.|[^`"\\])*`"", '$1N')
    # The expected value wrapped onto its own line ahead of the receiver, and the search method wrapped onto
    # the line after it. Both read the statement's earlier physical lines, like the Assert.True/False arm.
    if ($statementPrefix -match 'Assert\.(?:Contains|DoesNotContain|StartsWith|EndsWith|Equal)\s*\(\s*$') {
        $wrappedLiteral = "^(\s*)[`$@]{0,2}`"(?:\\.|[^`"\\])*`""
        if ($withoutNameof -match "$wrappedLiteral(?=\s*,\s*$sqlReceiverPath)" -or
            ($withoutNameof -match "$wrappedLiteral\s*,\s*$" -and $continuation -match "^\s*$sqlReceiverPath\s*[,)]")) {
            $withoutNameof = [regex]::Replace($withoutNameof, $wrappedLiteral, '$1N')
        }
    }
    if ($statementPrefix -match '(?:[Ss]ql|CommandText)\s*$') {
        $withoutNameof = [regex]::Replace($withoutNameof, "^(\s*\??\.\s*(?:Contains|StartsWith|EndsWith|IndexOf|LastIndexOf|Split)\s*\(\s*)[`$@]{0,2}`"(?:\\.|[^`"\\])*`"", '$1N')
    }
    # SQL text handed to a same-file helper that takes it. See Get-SqlHelperNames.
    if ($script:SqlHelperNames) {
        foreach ($helper in $script:SqlHelperNames) {
            $withoutNameof = [regex]::Replace($withoutNameof, "((?<![.\w])$([regex]::Escape($helper))\s*\((?:[^()`"]|\([^()]*\))*?)[`$@]{0,2}`"(?:\\.|[^`"\\])*`"", '$1N')
        }
    }
    # A JSON property name handed to a same-file reader. See Get-JsonPropertyHelperNames.
    if ($script:JsonPropertyHelperNames) {
        foreach ($helper in $script:JsonPropertyHelperNames) {
            $withoutNameof = [regex]::Replace($withoutNameof, "((?<![.\w])$([regex]::Escape($helper))\s*\((?:[^()`"]|\([^()]*\))*?)[`$@]{0,2}`"(?:\\.|[^`"\\])*`"", '$1N')
        }
    }
    $withoutNameof = [regex]::Replace($withoutNameof, '(ToString\s*\(\s*)"[^"]*"', '$1N')
    $withoutNameof = [regex]::Replace($withoutNameof, '(Assert\.Equal\s*\()"(?:\\.|[^"\\])*"(?=\s*,\s*[A-Z]\w*(?:\.\w+)+\s*\))', '$1N')
    # The same three label shapes, reached the way the code is actually formatted. Each stays anchored to
    # a token that names an exception or an assertion, so an ordinary literal on the line still sweeps:
    # a target-typed `new(...)` whose member returns an `*Exception`, an `*Exception` constructor passing
    # its message to `base(...)`, and an `Assert.True`/`Assert.False` message on a wrapped call's own line.
    $withoutNameof = [regex]::Replace($withoutNameof, '(\w*Exception\s+\w+\s*\([^)]*\)\s*=>\s*new\s*\(\s*)[$@]{0,2}"(?:\\.|[^"\\])*"', '$1N')
    $withoutNameof = [regex]::Replace($withoutNameof, '(\w*Exception\s*\([^)]*\)\s*:\s*base\s*\(\s*)[$@]{0,2}"(?:\\.|[^"\\])*"', '$1N')
    if ($statementPrefix -match 'Assert\.(?:True|False|SkipUnless|SkipWhen)\s*\(') {
        $withoutNameof = [regex]::Replace($withoutNameof, '^(\s*)[$@]{0,2}"(?:\\.|[^"\\])*"(?=\s*\)\s*;?\s*$)', '$1N')
    }
    # The same diagnostic once it wraps onto more than one line, which is how a message that names the
    # values it read is actually formatted. The arm above needs the literal to sit immediately before the
    # closing paren, so neither half of `"...text " + $"...{value}")` qualifies: the first line closes
    # nothing and the second opens with the `+`. Test-InAssertMessageArgument decides it from the comma
    # count at depth one instead, so a literal wrapped inside the CONDITION keeps reporting.
    if (Test-InAssertMessageArgument $statementPrefix) {
        $withoutNameof = [regex]::Replace(
            $withoutNameof,
            '^(\s*(?:\+\s*)?)[$@]{0,2}"(?:\\.|[^"\\])*"(?=\s*(?:\+\s*)?\)?\s*;?\s*$)',
            '$1N')
    }
    # An exception message long enough to wrap lands on its own line, where the single-line blanking above
    # cannot see the `new XException(` that owns it. Same shape as the Assert.True/False case directly
    # above, reached the same way - through the statement's earlier physical lines.
    if ($statementPrefix -match 'new\s+\w*Exception\s*\(\s*$') {
        $withoutNameof = [regex]::Replace($withoutNameof, '^(\s*)[$@]{0,2}"(?:\\.|[^"\\])*"', '$1N')
    }

    # The selector, route and wire-key positions. See the block above $playwrightSelectorCall for why each
    # of these names something outside the test, and why every one blanks a single argument rather than the
    # line it sits on.
    # The lookbehind is (?<!\w), NOT the (?<![.\w]) the SQL-helper arm uses: these are always called on a
    # receiver (`page.ClickAsync`, `locator.FillAsync`), so excluding a preceding dot would exclude every
    # real call site and the arm would never fire.
    $withoutNameof = [regex]::Replace(
        $withoutNameof,
        "((?<!\w)(?:$playwrightSelectorCall)\s*\(\s*)[`$@]{0,2}`"(?:\\.|[^`"\\])*`"",
        '$1N')
    $withoutNameof = [regex]::Replace(
        $withoutNameof,
        "((?<!\w)(?:$routeArgumentCall)\s*\(\s*)$routeLiteral",
        '$1N')
    # The same route argument, wrapped onto its own line. `_client.PostAsJsonAsync(` on one line and
    # `$"/chats/{id}/messages",` on the next is how these calls are actually formatted once they exceed the
    # line limit, and the single-line arm above cannot see the call that owns it. Reads the statement's
    # earlier physical lines, exactly as the Assert.Contains and new *Exception arms do. Anchored on the
    # call still: a literal wrapped under anything else keeps reporting.
    if ($statementPrefix -match "(?<!\w)(?:$routeArgumentCall)\s*\(\s*$") {
        $withoutNameof = [regex]::Replace($withoutNameof, "^(\s*)$routeLiteral", '$1N')
    }
    # The route reached through a CONSTRUCTOR rather than a verb method: `new HttpRequestMessage(
    # HttpMethod.Post, "/chats/messages")`. Same position as PostAsJsonAsync's first argument; the verb
    # argument is consumed by [^,]* so only the URI slot is blanked and a body literal still reports.
    $withoutNameof = [regex]::Replace(
        $withoutNameof,
        '(new\s+HttpRequestMessage\s*\(\s*[^,()]*,\s*)[$@]{0,2}"(?:\\.|[^"\\])*"',
        '$1N')
    # The URL assertion, which reaches its route through a matcher: ToHaveURLAsync(new Regex("/Admin/Users")).
    # A bare string form goes through the same arm.
    $withoutNameof = [regex]::Replace(
        $withoutNameof, '(ToHaveURLAsync\s*\(\s*(?:new\s+Regex\s*\(\s*)?)[$@]{0,2}"(?:\\.|[^"\\])*"', '$1N')
    # Browser code in the evaluate family's script position, inline or wrapped onto its own line.
    $withoutNameof = [regex]::Replace(
        $withoutNameof, '(' + $browserScriptOpening + ')[$@]{0,2}"(?:\\.|[^"\\])*"', '$1N')
    if ($statementPrefix -match ($browserScriptOpening + '$')) {
        $withoutNameof = [regex]::Replace($withoutNameof, '^(\s*)[$@]{0,2}"(?:\\.|[^"\\])*"', '$1N')
    }
    # A JSON property name and a SQL parameter name: the wire contract, not a specimen. AddWithValue is
    # anchored on the `@` prefix exactly as the Parameters[...] and Parameters.Contains arms above are, so an
    # ordinary string VALUE bound to a parameter still reports - which is the case the harness controls.
    $withoutNameof = [regex]::Replace(
        $withoutNameof, '((?:Try)?GetProperty\s*\(\s*)[$@]{0,2}"(?:\\.|[^"\\])*"', '$1N')
    $withoutNameof = [regex]::Replace($withoutNameof, '(AddWithValue\s*\(\s*)"@[^"]*"', '$1N')
    # An environment variable's NAME is the process contract the test reads or sets; its VALUE is data and
    # keeps reporting, because only the first argument is blanked.
    $withoutNameof = [regex]::Replace(
        $withoutNameof, '((?:Get|Set)EnvironmentVariable\s*\(\s*)[$@]{0,2}"(?:\\.|[^"\\])*"', '$1N')
    # An HTML attribute's NAME is the markup contract a Playwright assertion reads, the same position as a
    # selector; the expected VALUE beside it is data and keeps reporting, because only the first argument
    # is blanked.
    $withoutNameof = [regex]::Replace(
        $withoutNameof, '(ToHaveAttributeAsync\s*\(\s*)[$@]{0,2}"(?:\\.|[^"\\])*"', '$1N')
    # EnvironmentSetting.Count/Percent/Text name the variable in their FIRST argument, the same
    # position and the same process contract as (Get|Set)EnvironmentVariable above, so the same rule
    # reaches it. Anchored on the receiver, and only the first argument is blanked: the default and
    # the bounds that follow are data on this line and keep reporting.
    $withoutNameof = [regex]::Replace(
        $withoutNameof,
        '(EnvironmentSetting\.(?:Count|Percent|Text)\s*\(\s*)[$@]{0,2}"(?:\\.|[^"\\])*"', '$1N')
    # A SEPARATOR-API argument that carries no letter or digit is punctuation, not a datum. Rule 11 already
    # skips such a literal when interpolation or concatenation reaches it ("the test is the ABSENCE OF A
    # DATUM, not the syntax"); a plain `Replace(" ", string.Empty)` stripping formatting out of a TOTP key
    # is the same absence reached by a call. Anchored on BOTH the separator API (the same list rule 1 uses)
    # and the no-letter-no-digit shape, so `Replace("Phoenix", city)` still reports - which is the control.
    $withoutNameof = [regex]::Replace(
        $withoutNameof,
        '((?:Replace|Split|Join|Trim|TrimStart|TrimEnd|Concat)\s*\(\s*)"[^A-Za-z0-9"]*"',
        '$1N')
    # A `case` label must be a constant expression - the language compels it, exactly as it compels the
    # attribute argument the $AttributeReferencedNames arm already accepts. `case "GET":` has no end state.
    $withoutNameof = [regex]::Replace($withoutNameof, '^(\s*case\s+)[$@]{0,2}"(?:\\.|[^"\\])*"', '$1N')
    # An id SELECTOR is rule 10's mandated form, so rule 11 cannot also demand it be generated: the two
    # rules would contradict each other and generating the id would break the selector. Rule 10 wins,
    # because the id is a contract with production markup rather than a specimen - the same reasoning
    # that keeps a route, a claim type and a header out of this rule. Deliberately narrow: only a
    # literal that is ENTIRELY `#id` or an `[id...=...]` attribute selector. A class, a tag, an xpath or
    # a text selector is not matched here and keeps reporting, which is also what rule 10 wants, and a
    # literal elsewhere on the same line is untouched.
    $withoutNameof = [regex]::Replace(
        $withoutNameof, '[$@]{0,2}"#[\w-]+"', 'N')
    $withoutNameof = [regex]::Replace(
        $withoutNameof, '[$@]{0,2}"\[id[\^$*~|]?=[^"\]]*\]"', 'N')
    # The expected side of an assertion against a *Url/*Uri receiver, the exact sibling of the *sql arm
    # above. `Assert.DoesNotContain("/Account/Login", page.Url, ...)` pins the route the browser must not be
    # on; the route is the contract, not a specimen.
    $withoutNameof = [regex]::Replace(
        $withoutNameof,
        "(Assert\.(?:Contains|DoesNotContain|StartsWith|EndsWith|Equal)\s*\()[`$@]{0,2}`"(?:\\.|[^`"\\])*`"(?=\s*,\s*$urlReceiverPath)",
        '$1N')
    # The actual side reached by CONSTRUCTING a Uri: `Assert.Equal("/Admin/Clients", new Uri(page.Url).AbsolutePath)`.
    # $urlReceiverPath cannot describe this one - it requires the Url/Uri token to END the receiver path,
    # and `new Uri(...)` puts it in the middle with a space in front - so the idiom needs its own arm.
    # `new Uri(x)` is definitionally a URL, which is what makes the expected side contract text here.
    $withoutNameof = [regex]::Replace(
        $withoutNameof,
        '(Assert\.(?:Equal|Contains|DoesNotContain|StartsWith|EndsWith)\s*\()[$@]{0,2}"(?:\\.|[^"\\])*"(?=\s*,\s*new\s+Uri\s*\()',
        '$1N')
    # The same receiver asking the question itself: `url.Contains("/Account/Login", StringComparison...)`
    # inside a WaitForURLAsync predicate, and `page.Url.Contains("localhost:9999", ...)`.
    $withoutNameof = [regex]::Replace(
        $withoutNameof,
        "($urlReceiverPath\s*\??\.\s*(?:Contains|StartsWith|EndsWith|IndexOf|LastIndexOf|Split)\s*\(\s*)[`$@]{0,2}`"(?:\\.|[^`"\\])*`"",
        '$1N')

    foreach ($m in (Get-CodeLiterals $withoutNameof)) {
        # An interpolated string that generates its own value is not a magic literal - telling the author
        # to "generate" a value that is already generated is the same compliance penalty as above.
        if ($m.Value -match $generatorCallPattern) { continue }
        # An [InlineData] composite-format template is filled from generated values at the call site; its
        # `{n}` slots are what make it a template rather than a specimen.
        if ($isAttribute -and $m.Value -match '\{\d+\}') { continue }
        # A literal whose segments carry no letter or digit (`$"{scheme} {token}"`, `$"/{path}"`,
        # `searchKeyword + "%"`) is glue between generated values; there is no datum left to generate.
        # The test is the ABSENCE OF A DATUM, not the syntax that reaches it: this used to require a `$`
        # immediately before the literal, so `$"{keyword}%"` was excused and the identical
        # `keyword + "%"` was charged, and the author's choice of operator decided the verdict. The
        # worded control in the harness is what keeps the widening honest - a neighboring `+` is not a
        # license on its own, or `keyword + " AND "` would go quiet too.
        # Strip the holes INNERMOST-FIRST until nothing changes: `\{[^{}]*\}` cannot match a hole that
        # contains another one, so a single pass leaves `{Wrap(Name, $"  ")}` behind and its identifiers
        # read as data. One pass was enough while holes never nested; a nested interpolated string is
        # exactly the case where they do.
        $withoutHoles = $m.Value
        while ($true) {
            $stripped = [regex]::Replace($withoutHoles, '\{[^{}]*\}', '')
            if ($stripped -eq $withoutHoles) { break }
            $withoutHoles = $stripped
        }

        $noDatumLeft = $withoutHoles -notmatch '[A-Za-z0-9]'
        $isInterpolated = $m.Index -gt 0 -and $withoutNameof[$m.Index - 1] -eq '$'
        $isConcatenated = $withoutNameof.Substring(0, $m.Index) -match '\+\s*$' -or
            $withoutNameof.Substring($m.Index + $m.Length) -match '^\s*\+'
        if ($noDatumLeft -and ($isInterpolated -or $isConcatenated)) { continue }
        $disp = Get-LiteralDisposition $m.Value $false $relPath
        if ($disp[1] -eq 'keep-boundary') { continue }
        $cat = if ($isAttribute) { "inlinedata-$($disp[0])" } else { $disp[0] }
        Add-Row $repo $relPath $lineNum $area 11 $cat $disp[1] $raw
    }

    $noStr = Remove-LiteralBodies $withoutNameof
    # The arguments to a random primitive are the generator's DOMAIN, not test data. Asking an author to
    # "generate" the bounds of a generator is the compliance penalty rule 11 exists to avoid, and it has no
    # end: the replacement bounds would report too. Only the argument list is blanked, so a magic number
    # anywhere else on the line still fires. Deliberately skipped when the arguments contain a nested call
    # (`Random.Shared.Next(0, items.Count())`), because the inner call's own literals are not bounds and
    # failing to report is the worse direction.
    $noStr = [regex]::Replace($noStr, '(Random\.Shared\.(?:Next|NextInt64|NextDouble|NextSingle)\s*\()[^()]*\)', '$1)')
    # The same argument, one call further out. `Generated.LowercaseToken(6)` asks the shared generator for
    # a six-character token: the 6 is the generator's DOMAIN, exactly as the bounds above are, and
    # CODE-STYLE.md names `Shared.Testing.Generated` as rule 11's END STATE. Charging the author a
    # magic-number row for calling it is the compliance penalty this rule exists to avoid, and it has no
    # end - the replacement length would report too. Anchored on the RECEIVER, so an argument to anything
    # else still reports, and only the argument list is blanked, so a magic number elsewhere on the line
    # still fires. Skipped where the arguments contain a nested call, for the same reason as above. The
    # receiver is the anchor, so a per-project generator class (`TestValues`) earns none of this.
    $noStr = [regex]::Replace($noStr, '(Generated\.\w+\s*\()[^()]*\)', '$1)')
    foreach ($m in [regex]::Matches($noStr, '(?<![\w.])-?\d+(?:\.\d+)?[dmfLu]?(?![\w.])')) {
        $disp = Get-LiteralDisposition $m.Value $true $relPath
        if ($disp[1] -eq 'keep-boundary') { continue }
        $cat = if ($isAttribute) { "inlinedata-$($disp[0])" } else { $disp[0] }
        Add-Row $repo $relPath $lineNum $area 11 $cat $disp[1] $raw
    }
}

Write-Host "Scanning $WorkspaceRoot for CODE-STYLE.md rule violations (rules: $($Rule -join ', ')) ..."

# Two limits a consumer of these counts has to know about, both properties of a line scanner.
#
# Every arm is Add-Row followed by return, so a line is reported under the FIRST category it matches and a
# category count is not a violation count. Four of the five `[id^='details-']`.First sites in Identity carry
# HasText on the same line and so land as text-selector, never reaching the positional arm - still flagged,
# but not under the category you would predict.
#
# The positional arm also requires locator( on the same line, so a locator held in a variable escapes it:
# `const content = page.locator(...)` on one line and `content.first()` on the next is invisible. api.spec.ts:30
# is the live example. Rule 10 has no getBy* arm at all: a `getByRole`/`getByText` selector is invisible to
# this detector, so a zero here is a statement about the scanner, not about the suite.
#
# Scope comes from Tools/Shared/ScanScopeHelper.ps1 so this script and Get-CommentCatalog.ps1 cannot
# drift apart again: they carried two independent lists (15 extensions there, 3 here), so "N rows
# fleet-wide" from the two scripts silently meant two different fleets.
#
# What this script IMPLEMENTS is still narrower than what the fleet scans - only CSharp and TypeScript
# have rule scanners below. That gap is now REPORTED at the end instead of being an absence, because a
# language with no scanner contributes zero rows and a zero reads as compliance. Adding a language here
# means writing its scanners, not adding its extension: the rule regexes are C#/TypeScript-shaped and
# running them over Python or SQL manufactures rows rather than finding them.
$implementedLanguages = @('CSharp', 'TypeScript')
$exemptRepos = @('Tools', 'Harness', 'psnpy')
$extMap = Get-ScanExtensionMap
$rule14Extensions = @('.css', '.html', '.cshtml', '.razor', '.astro')
$scanRoot = if ($RepoPath) { $RepoPath } elseif ($Repo) { Join-Path $WorkspaceRoot $Repo } else { $WorkspaceRoot }
if (-not (Test-Path -LiteralPath $scanRoot -PathType Container)) { throw "Repo '$Repo' is not a directory under $WorkspaceRoot." }
$walked = @(Get-ChildItem -Path $scanRoot -Recurse -File -ErrorAction SilentlyContinue |
    Where-Object {
        ($extMap.ContainsKey($_.Extension) -or $rule14Extensions -contains $_.Extension) -and -not (Test-Excluded $_) -and
        $exemptRepos -notcontains (Get-RepoName $_.FullName $WorkspaceRoot)
    })
$inScope = @($walked | Where-Object { $extMap.ContainsKey($_.Extension) })
$files = @($inScope | Where-Object { $implementedLanguages -contains $extMap[$_.Extension] })
$scannedByRule14 = if ($Rule -contains 14) { @('Css') } else { @() }
$unscanned = @($inScope | Where-Object {
        $implementedLanguages -notcontains $extMap[$_.Extension] -and $scannedByRule14 -notcontains $extMap[$_.Extension]
    })

# Rule 11, "production text is borrowed, never restated": a test literal that repeats a string production
# emits is a second copy that drifts silently. This pass runs over C#, TypeScript and Python alike, because
# the check is a set comparison rather than a language-shaped sweep: index every single-line literal in a
# repo's production code, then report every test literal in the same repo that equals one or renders one
# of its templates. A template (`$"Invalid '{key}'."`, a backtick string, an f-string) is indexed as a
# pattern whose holes match anything, which is how a rendered copy (`"Invalid 'OrderQueue'."`) is caught.
#
# Three shapes are not copies. A module specifier (`from './settings'`, `vi.mock('pino')`) names a file,
# not text. A literal spanning lines is a payload judged elsewhere. And the pin shape rule 11 mandates, a
# literal asserted against a named production constant (`Assert.Equal("sub", Claims.Subject)`,
# `expect(LOG_INDEX).toBe(...)`, `assert ACTION_X == "..."`), is the test that owns the literal; the C#
# sweep blanks the same shape at Scan-TestLiteralLine.
#
# Python tests are recognised here by path because Get-ScanArea reads `Curator\tests\` as production, and
# that boundary is shared with the comment catalog, which must not move with this detector.
$copyScanExtensions = @('.cs', '.py') + $typeScriptExtensions
$copyHole = [string][char]0
$copyMinimumLength = 6
$moduleSpecifierPrefix = '(?:\bfrom|\bimport|\brequire\s*\(|\bvi\.(?:mock|doMock|importActual)\s*\(|\bjest\.mock\s*\(|\bdeclare\s+module)\s*$'

function Get-CopyScanArea([string]$relPath) {
    if ($relPath -match '\.py$') {
        if ($relPath -match '\\tests?\\' -or $relPath -match '(^|\\)(test_\w+|\w+_test|conftest)\.py$') {
            return 'test-unit'
        }
        return 'prod'
    }
    return Get-Area $relPath
}

function Test-CopyCandidate([string]$text) {
    $fixed = $text.Replace($copyHole, '')
    return $fixed.Length -ge $copyMinimumLength -and $fixed -match '[A-Za-z]' -and $fixed -notmatch '^__\w+__$'
}

function New-CopyLiteral([int]$line, [int]$column, [int]$length, [string]$text) {
    return [pscustomobject]@{ Line = $line; Column = $column; Length = $length; Text = $text }
}

function Get-CSharpCopyLiterals($lines) {
    $found = New-Object System.Collections.Generic.List[object]
    $rawInside = (Get-RawStringMap $lines).Inside
    for ($i = 0; $i -lt $lines.Count; $i++) {
        if ($rawInside.Contains($i + 1) -or $lines[$i] -match '"{3,}') { continue }
        $code = Remove-LineComment $lines[$i]
        foreach ($literal in (Get-CodeLiterals $code)) {
            $index = $literal.Index
            $interpolated = ($index -gt 0 -and $code[$index - 1] -eq '$') -or
                ($index -gt 1 -and $code[$index - 2] -eq '$' -and $code[$index - 1] -eq '@')
            $body = $literal.Value.Substring(1, $literal.Value.Length - 2)
            if ($interpolated) {
                $body = [regex]::Replace($body, '(?<!\{)\{(?!\{)(?:[^{}]|\{[^{}]*\})*\}', $copyHole)
            }
            $found.Add((New-CopyLiteral ($i + 1) $index $literal.Length $body))
        }
    }
    return , $found
}

function Get-TsCopyLiterals([string]$text) {
    $found = New-Object System.Collections.Generic.List[object]
    $line = 1
    $lineStart = 0
    $i = 0
    $n = $text.Length
    while ($i -lt $n) {
        $c = $text[$i]
        if ($c -eq "`n") { $line++; $lineStart = $i + 1; $i++; continue }
        if ($c -eq '/' -and $i + 1 -lt $n -and $text[$i + 1] -eq '/') {
            $newline = $text.IndexOf("`n", $i)
            if ($newline -lt 0) { break }
            $i = $newline
            continue
        }
        if ($c -eq '/' -and $i + 1 -lt $n -and $text[$i + 1] -eq '*') {
            $end = $text.IndexOf('*/', $i + 2)
            if ($end -lt 0) { break }
            for ($k = $i; $k -lt $end; $k++) {
                if ($text[$k] -eq "`n") { $line++; $lineStart = $k + 1 }
            }
            $i = $end + 2
            continue
        }
        if ($c -ne "'" -and $c -ne '"' -and $c -ne '`') { $i++; continue }

        $startLine = $line
        $column = $i - $lineStart
        $prefix = $text.Substring($lineStart, $column)
        $body = New-Object System.Text.StringBuilder
        $j = $i + 1
        $closed = $false
        $spansLines = $false
        while ($j -lt $n) {
            $ch = $text[$j]
            if ($ch -eq '\' -and $j + 1 -lt $n) {
                [void]$body.Append($ch).Append($text[$j + 1])
                $j += 2
                continue
            }
            if ($ch -eq "`n") {
                if ($c -ne '`') { break }
                $spansLines = $true
                $line++
                $lineStart = $j + 1
            }
            if ($ch -eq $c) { $closed = $true; break }
            if ($c -eq '`' -and $ch -eq '$' -and $j + 1 -lt $n -and $text[$j + 1] -eq '{') {
                $depth = 1
                $j += 2
                while ($j -lt $n -and $depth -gt 0) {
                    $h = $text[$j]
                    if ($h -eq '{') { $depth++ }
                    elseif ($h -eq '}') { $depth-- }
                    elseif ($h -eq "`n") { $line++; $lineStart = $j + 1; $spansLines = $true }
                    $j++
                }
                [void]$body.Append($copyHole)
                continue
            }
            [void]$body.Append($ch)
            $j++
        }
        if ($closed -and -not $spansLines -and $prefix -notmatch $moduleSpecifierPrefix) {
            $found.Add((New-CopyLiteral $startLine $column ($j - $i + 1) $body.ToString()))
        }
        $i = if ($closed) { $j + 1 } else { $j }
    }
    return , $found
}

function Get-PyCopyLiterals([string]$text) {
    $found = New-Object System.Collections.Generic.List[object]
    $line = 1
    $lineStart = 0
    $i = 0
    $n = $text.Length
    while ($i -lt $n) {
        $c = $text[$i]
        if ($c -eq "`n") { $line++; $lineStart = $i + 1; $i++; continue }
        if ($c -eq '#') {
            $newline = $text.IndexOf("`n", $i)
            if ($newline -lt 0) { break }
            $i = $newline
            continue
        }
        if ($c -ne "'" -and $c -ne '"') { $i++; continue }

        $p = $i - 1
        while ($p -ge $lineStart -and $i - $p -le 2 -and $text[$p] -match '[rRbBfFuU]') { $p-- }
        $isFormatted = $text.Substring($p + 1, $i - $p - 1) -match '[fF]'
        $column = $i - $lineStart
        $triple = $i + 2 -lt $n -and $text[$i + 1] -eq $c -and $text[$i + 2] -eq $c
        if ($triple) {
            $end = $text.IndexOf([string]::new($c, 3), $i + 3)
            if ($end -lt 0) { break }
            $body = $text.Substring($i + 3, $end - $i - 3)
            if ($body.Contains("`n")) {
                for ($k = $i; $k -lt $end; $k++) {
                    if ($text[$k] -eq "`n") { $line++; $lineStart = $k + 1 }
                }
            }
            else {
                $found.Add((New-CopyLiteral $line $column ($end - $i + 3) $body))
            }
            $i = $end + 3
            continue
        }

        $body = New-Object System.Text.StringBuilder
        $j = $i + 1
        $closed = $false
        while ($j -lt $n) {
            $ch = $text[$j]
            if ($ch -eq "`n") { break }
            if ($ch -eq '\' -and $j + 1 -lt $n) {
                [void]$body.Append($ch).Append($text[$j + 1])
                $j += 2
                continue
            }
            if ($ch -eq $c) { $closed = $true; break }
            if ($isFormatted -and $ch -eq '{') {
                if ($j + 1 -lt $n -and $text[$j + 1] -eq '{') {
                    [void]$body.Append('{{')
                    $j += 2
                    continue
                }
                $depth = 1
                $j++
                while ($j -lt $n -and $depth -gt 0 -and $text[$j] -ne "`n") {
                    if ($text[$j] -eq '{') { $depth++ }
                    elseif ($text[$j] -eq '}') { $depth-- }
                    $j++
                }
                [void]$body.Append($copyHole)
                continue
            }
            [void]$body.Append($ch)
            $j++
        }
        if ($closed) {
            $found.Add((New-CopyLiteral $line $column ($j - $i + 1) $body.ToString()))
        }
        $i = if ($closed) { $j + 1 } else { $j }
    }
    return , $found
}

function Get-CopyLiterals([System.IO.FileInfo]$file, [string]$text, $lines) {
    switch ($file.Extension) {
        '.cs' { return Get-CSharpCopyLiterals $lines }
        '.py' { return Get-PyCopyLiterals $text }
        default { return Get-TsCopyLiterals $text }
    }
}

$copyConstantPath = @{
    '.cs' = '[A-Z]\w*(?:\.\w+)+'
    '.ts' = '(?:[A-Z][A-Z0-9_]*|[A-Z]\w*(?:\.\w+)+)'
    '.py' = '(?:_?[A-Z][A-Z0-9_]*|\w+\._?[A-Z][A-Z0-9_]*)'
}
foreach ($typeScriptExtension in $typeScriptExtensions) { $copyConstantPath[$typeScriptExtension] = $copyConstantPath['.ts'] }

# `typeof value === 'string'` names a type the language defines; neither side owns the word, so it is never a
# copy. Only the comparison operand position is excused: the same word anywhere else still reports.
$typeofComparison = '\btypeof\s+[\w$.?\[\]]+\s*[!=]==?\s*$'

$pyQuotedLiteral = [regex]::new('"(?:\\.|[^"\\])*"|''(?:\\.|[^''\\])*''')
$pyAdjacentLiterals = '(?:[rbuRBUfF]{0,2}(?:"(?:\\.|[^"\\])*"|''(?:\\.|[^''\\])*'')\s*)*'

# A Python literal inside brackets opened on an earlier line is positioned by the statement that opened them:
# `cur.execute(`, `@pytest.mark.parametrize(` or an `assert (A, B) == (` pin spread over several lines places
# it exactly as the one-line form would. Walks back to the line where the literal's bracket depth first opens.
function Get-PyStatementPrefix([string[]]$lines, [int]$lineNumber, [string]$before) {
    $accumulated = $before.Trim()
    $earliest = [Math]::Max(0, $lineNumber - 41)
    for ($k = $lineNumber - 2; $k -ge $earliest; $k--) {
        $accumulated = $lines[$k].Trim() + ' ' + $accumulated
        $code = $pyQuotedLiteral.Replace($accumulated, "''")
        $depth = ([regex]::Matches($code, '[(\[{]')).Count - ([regex]::Matches($code, '[)\]}]')).Count
        if ($depth -gt 0 -and $lines[$k].Trim() -notmatch '^[)\]}]') { return $accumulated }
    }
    return $null
}

function Test-CopyIsPin([string]$extension, [string]$before, [string]$after) {
    $constant = $copyConstantPath[$extension]
    switch ($extension) {
        '.cs' {
            return ($before -cmatch 'Assert\.Equal\s*\(\s*[$@]*$' -and $after -cmatch "^\s*,\s*$constant\s*\)") -or
                ($before -cmatch 'Assert\.Equal\s*\(\s*\[[^\]]*$' -and $after -cmatch "^[^\]]*\]\s*,\s*$constant\s*\)")
        }
        '.py' {
            $quotePrefix = '[rbuRBUfF]{0,2}$'
            # A pin against a named production constant, including a tuple or list of them on either side.
            return ($before -cmatch "\bassert\s+$constant\s*==\s*$quotePrefix") -or
                ($before -cmatch "\bassert\s+[rbuRBU]{0,2}$" -and $after -cmatch "^\s*==\s*$constant\s*(?:,|$)") -or
                ($before -cmatch "\bassert\s+$constant\s*==\s*[(\[{].*$quotePrefix") -or
                ($before -cmatch "\bassert\s+\(\s*$constant(?:\s*,\s*$constant)*\s*,?\s*\)\s*==\s*[(\[{].*$quotePrefix") -or
                # SQL checked against the statement production built, or a test's own SQL handed to the driver.
                ($before -match "(?:\bassert|\(|\bany\s*\()\s*(?:not\s+)?$quotePrefix" -and $after -match '^\s*(?:not\s+)?in\s+[\w.\[\]]*(?:sql|statement)\w*\b') -or
                ($before -match ('\.(?:execute|executemany|fetch|fetchrow|fetchval)\s*\(\s*' + $pyAdjacentLiterals + $quotePrefix)) -or
                # pytest's argnames name the test's own parameters; a subscript reads a key back out of a record.
                ($before -match "@pytest\.mark\.parametrize\s*\(\s*$quotePrefix") -or
                ($before -match "[\w)\]]\s*\[\s*$quotePrefix" -and $after -match '^\s*\]')
        }
        default {
            return $before -cmatch "expect\(\s*$constant\s*\)\s*\.\s*(?:toBe|toEqual|toStrictEqual)\s*\(\s*$" -or
                $before -cmatch $typeofComparison
        }
    }
}

if ($Rule -contains 11) {
    $copyFiles = @($inScope | Where-Object { $copyScanExtensions -contains $_.Extension })
    $productionText = @{}
    $testFiles = New-Object System.Collections.Generic.List[object]
    foreach ($f in $copyFiles) {
        $repo = Get-RepoName $f.FullName $WorkspaceRoot
        $rel = $f.FullName.Substring($WorkspaceRoot.Length).TrimStart('\')
        $area = Get-CopyScanArea $rel
        $text = Get-Content -LiteralPath $f.FullName -Raw -ErrorAction SilentlyContinue
        if (-not $text) { continue }
        $text = $text -replace "`r`n", "`n"
        $lines = $text -split "`n"
        $literals = Get-CopyLiterals $f $text $lines
        if ($area -ne 'prod') {
            $testFiles.Add([pscustomobject]@{ Repo = $repo; Rel = $rel; Area = $area; File = $f; Lines = $lines; Literals = $literals })
            continue
        }
        if (-not $productionText.ContainsKey($repo)) {
            $productionText[$repo] = [pscustomobject]@{
                Exact    = New-Object 'System.Collections.Generic.HashSet[string]' ([StringComparer]::Ordinal)
                Patterns = New-Object System.Collections.Generic.List[object]
            }
        }
        foreach ($literal in $literals) {
            if (-not (Test-CopyCandidate $literal.Text)) { continue }
            if (-not $productionText[$repo].Exact.Add($literal.Text)) { continue }
            if ($literal.Text.Contains($copyHole)) {
                $segments = $literal.Text.Split($copyHole)
                $anchor = ($segments | Sort-Object Length -Descending | Select-Object -First 1)
                $pattern = '^' + (($segments | ForEach-Object { [regex]::Escape($_) }) -join '.+') + '$'
                $productionText[$repo].Patterns.Add([pscustomobject]@{ Anchor = $anchor; Regex = [regex]::new($pattern) })
            }
        }
    }

    foreach ($testFile in $testFiles) {
        $index = $productionText[$testFile.Repo]
        if ($null -eq $index) { continue }
        # A genuine constants file is where a value this fleet does not control earns its name (CODE-STYLE.md
        # rule 11): its declarations are the owner, not a restatement, in either language. Whether a value
        # belongs there at all is the ownership question the rule asks a human, not this pass.
        if (Test-OwnerConstantsFile $testFile.File $testFile.Rel) { continue }
        $pinnedLines = if ($testFile.File.Extension -eq '.cs') { Get-PinnedLiteralLines $testFile.Lines } else { $null }
        foreach ($literal in $testFile.Literals) {
            if (-not (Test-CopyCandidate $literal.Text)) { continue }
            if ($pinnedLines -and $pinnedLines.Contains($literal.Line)) { continue }
            $copied = $index.Exact.Contains($literal.Text)
            if (-not $copied -and -not $literal.Text.Contains($copyHole)) {
                foreach ($template in $index.Patterns) {
                    if ($literal.Text.Contains($template.Anchor) -and $template.Regex.IsMatch($literal.Text)) {
                        $copied = $true
                        break
                    }
                }
            }
            if (-not $copied) { continue }
            $lineText = $testFile.Lines[$literal.Line - 1]
            if ($testFile.File.Extension -eq '.cs' -and $lineText -match $frameworkAttributePattern) { continue }
            # A statement that is only a triple-quoted string is a docstring, a test's title.
            if ($testFile.File.Extension -eq '.py' -and $lineText.Trim() -match '^[rbuRBU]{0,2}("""|'''''').*("""|'''''')$') { continue }
            $before = $lineText.Substring(0, [Math]::Min($literal.Column, $lineText.Length))
            # A suite or test title names the case, as a C# framework attribute or a Python docstring does.
            if ($testFile.File.Extension -in '.ts', '.tsx' -and $before -match '\b(?:test|it|describe)(?:\.\w+)*\s*\(\s*$') { continue }
            $afterStart = [Math]::Min($literal.Column + $literal.Length, $lineText.Length)
            $after = $lineText.Substring($afterStart)
            if (Test-CopyIsPin $testFile.File.Extension $before $after) { continue }
            if ($testFile.File.Extension -eq '.py') {
                $statementPrefix = Get-PyStatementPrefix $testFile.Lines $literal.Line $before
                if ($statementPrefix -and (Test-CopyIsPin '.py' $statementPrefix $after)) { continue }
            }
            Add-Row $testFile.Repo $testFile.Rel $literal.Line $testFile.Area 11 'copied-production-text' 'borrow-from-production' $lineText
        }
    }
}

# Rule 12 over Python tests, recognised by the same path test as the copied-production-text pass above,
# because Get-ScanArea reads `Curator\tests\` as production.
if ($Rule -contains 12) {
    foreach ($pyFile in @($inScope | Where-Object { $_.Extension -eq '.py' })) {
        $pyRel = $pyFile.FullName.Substring($WorkspaceRoot.Length).TrimStart('\')
        if ((Get-CopyScanArea $pyRel) -ne 'test-unit') { continue }
        $pyText = Get-Content -LiteralPath $pyFile.FullName -Raw -ErrorAction SilentlyContinue
        if (-not $pyText) { continue }
        Scan-PyTestLogic (Get-RepoName $pyFile.FullName $WorkspaceRoot) $pyRel ($pyText -split "`r?`n")
    }
}

$total = $files.Count
$n = 0
foreach ($f in $files) {
    $n++
    if ($n % 500 -eq 0) { Write-Host "  $n / $total files scanned..." }

    $repo = Get-RepoName $f.FullName $WorkspaceRoot
    $rel = $f.FullName.Substring($WorkspaceRoot.Length).TrimStart('\')
    $area = Get-Area $rel
    # Rule 11 sweeps EVERY test area, not just the unit one. A literal encodes a shape wherever it sits, so
    # an E2E fixture, a helper under Tests/ and a Playwright or Vitest config are test data exactly as a
    # unit spec's are. Get-Area reads a path's e2e segment BEFORE its project name, so moving a file one
    # folder up changes whether it is scanned at all.
    $sweepsTestData = $area -ne 'prod'

    $text = Get-Content -LiteralPath $f.FullName -Raw -ErrorAction SilentlyContinue
    if (-not $text) { continue }
    $lines = $text -split "`r?`n"

    if (($Rule -contains 12) -and $area -eq 'test-unit' -and $f.Extension -eq '.cs') {
        Scan-TestLogic $repo $rel $area $lines
        Scan-GeneratedTheoryData $repo $rel $area $lines
    }
    if (($Rule -contains 12) -and $area -eq 'test-unit' -and $typeScriptExtensions -contains $f.Extension) {
        Scan-TsTestLogic $repo $rel $area $lines
    }

    # UNIT ONLY. The markers are required in the unit tier and forbidden in every other, so this gate and
    # the comment catalog's marker allowance must name the same single area: a tier that is required to
    # carry markers by one scanner and forbidden to by the other is unfixable by any edit.
    if (($Rule -contains 18) -and $area -eq 'test-unit' -and $f.Extension -eq '.cs') {
        Scan-TestAaa $repo $rel $area $lines
    }

    # Raw-string blocks are mapped per file and consumed by the rule 11 scanners, which is why this is
    # script-scoped state rather than a parameter - the scanners are called per line.
    # Whether this file is a genuine constants class is a property of the WHOLE file, so it is decided once
    # here rather than per line, and for the same reason as the raw-string map it is script-scoped state.
    $script:TsGeneratorNames = $null
    $script:GeneratedLocalNames = Get-GeneratedLocalNames $text
    $script:TsPreviousCode = $null
    $script:TsFileUsesRuleTester = $text -match '\bnew\s+RuleTester\s*\('
    $script:TsFileText = $text
    $script:TsTestTitleAliases = @([regex]::Matches($text, '\btest\s+as\s+([A-Za-z_$][\w$]*)') | ForEach-Object { $_.Groups[1].Value })
    $script:TsExpectDepth = 0
    $script:TsPreviousWasExpectMessage = $false
    $script:TsTemplateStack = New-Object System.Collections.Generic.List[string]
    $script:CurrentFileIsTsConstantsOwner = ($typeScriptExtensions -contains $f.Extension) -and
        $rel -match $tsConstantsFilePattern -and (Test-TsConstantsOnlyFile $f.FullName)
    $script:CurrentFileIsConstantsOnly = $false
    if ($f.Extension -eq '.cs' -and $rel -match $constantsFilePattern) {
        $script:CurrentFileIsConstantsOnly = Test-ConstantsOnlyFile $f.FullName
    }

    # Which identifiers an attribute in this file names, for the same reason: the attribute and the
    # declaration it references sit on different lines, so it is decided once per file.
    $script:AttributeReferencedNames = if ($f.Extension -eq '.cs') {
        Get-AttributeReferencedNames $text
    }
    else {
        New-Object 'System.Collections.Generic.HashSet[string]' ([StringComparer]::Ordinal)
    }

    # Which identifiers this file uses as default parameter values, decided once per file because the
    # signature and the declaration sit on different lines.
    $script:DefaultParameterNames = if ($f.Extension -eq '.cs') {
        Get-DefaultParameterNames $text
    }
    else {
        New-Object 'System.Collections.Generic.HashSet[string]' ([StringComparer]::Ordinal)
    }

    # Which consts this file hands to EnvironmentSetting as a default or a bound, decided once per file
    # because the declaration and the call sit on different lines.
    $script:BrowserScriptConstNames = if ($f.Extension -eq '.cs' -and $sweepsTestData) {
        Get-BrowserScriptConstNames $text
    }
    else {
        New-Object 'System.Collections.Generic.HashSet[string]' ([StringComparer]::Ordinal)
    }

    $script:ConfigSchemaConstNames = if ($f.Extension -eq '.cs' -and $sweepsTestData) {
        Get-ConfigSchemaConstNames $text
    }
    else {
        New-Object 'System.Collections.Generic.HashSet[string]' ([StringComparer]::Ordinal)
    }

    # Which literal lines are the expected side of a rule-18-shaped pin test, decided once per file because
    # the literal and the production value it pins sit in different statements.
    $script:PinnedLiteralLines = if ($f.Extension -eq '.cs' -and $sweepsTestData) {
        Get-PinnedLiteralLines $lines
    }
    else {
        $null
    }
    # SQL and assertion-message constants, and the helpers that take SQL text. See Get-ContractTextLines and
    # Get-SqlHelperNames.
    $script:ContractTextLines = if ($f.Extension -eq '.cs' -and $sweepsTestData) {
        Get-ContractTextLines $lines
    }
    else {
        $null
    }
    $script:ParsedStringLocals = if ($f.Extension -eq '.cs' -and $area -eq 'prod') {
        Get-ParsedStringLocals $text
    }
    else {
        $null
    }
    $script:SqlHelperNames = if ($f.Extension -eq '.cs' -and $sweepsTestData) {
        Get-SqlHelperNames $lines
    }
    else {
        $null
    }
    $script:JsonPropertyHelperNames = if ($f.Extension -eq '.cs' -and $sweepsTestData) {
        Get-JsonPropertyHelperNames $lines
    }
    else {
        $null
    }

    $script:RawStringInside = $null
    if ($f.Extension -eq '.cs') {
        $rawMap = Get-RawStringMap $lines
        $script:RawStringInside = $rawMap.Inside
        if (($Rule -contains 11) -and $sweepsTestData) {
            foreach ($start in $rawMap.Starts) {
                # A raw block that is a named SQL statement is the same end state Get-ContractTextLines
                # accepts for a regular string: a const named *Sql whose text opens with a statement keyword.
                $opening = $lines[$start - 1]
                $firstBodyLine = if ($start -lt $lines.Count) { $lines[$start] } else { '' }
                if ($opening -match '\bconst\s+string\s+\w*Sql\s*=\s*\$*"{3,}\s*$' -and
                    $opening -cmatch 'Sql\s*=' -and
                    $firstBodyLine -cmatch '^\s*(?:SELECT|INSERT INTO|UPDATE|DELETE FROM|WITH)\s') {
                    continue
                }
                if (Test-BrowserScriptBlock $lines $start) { continue }
                Add-Row $repo $rel $start $area 11 'raw-string-block' 'needs-triage' $opening
            }
            foreach ($start in $rawMap.VerbatimStarts) {
                if (Test-BrowserScriptBlock $lines $start) { continue }
                Add-Row $repo $rel $start $area 11 'verbatim-string-block' 'needs-triage' $lines[$start - 1]
            }
        }
    }

    if (($Rule -contains 11) -and $sweepsTestData -and $f.Extension -eq '.cs') {
        Scan-PerFileGenerators $repo $rel $area $lines
    }

    # Which logger parameters exist only to satisfy a base constructor is a property of the whole file - the
    # declaration, the base initializer and any assignment sit on different lines - so it is decided once.
    $mandatedLoggerParameters = Get-BasePassThroughLoggerParameters $text
    # The blanked lines of the current statement before this one (reset at `;`, `{` or `}`), capped at
    # eight, so a wrapped query's receiver is visible from its continuation lines.
    $statementPrefix = ''

    for ($i = 0; $i -lt $lines.Count; $i++) {
        $raw = $lines[$i]
        if ($raw.Trim() -eq '') { continue }
        $lineNum = $i + 1

        if ($f.Extension -eq '.cs') {
            # Four lines is enough to close an Allman-wrapped argument list; the balance scan stops at the
            # call's own closing paren, so a longer window cannot pull in a later call's comparison.
            $continuation = ''
            if ($i + 1 -lt $lines.Count) {
                $window = $lines[($i + 1)..([Math]::Min($i + 4, $lines.Count - 1))]
                $continuation = ($window | ForEach-Object { Remove-LiteralBodies (Remove-LineComment $_) }) -join "`n"
            }
            # Captured before the update below, so it is the statement BEFORE this line - the rule 11
            # scanner further down reads the same value.
            $prefixBefore = $statementPrefix
            Scan-CSharpLine $repo $rel $lineNum $area $raw $continuation $mandatedLoggerParameters $prefixBefore
            $blanked = Remove-LiteralBodies (Remove-LineComment $raw)
            if ($blanked -match '[;{}]\s*$') {
                $statementPrefix = ''
            }
            else {
                $prefixLines = @(($statementPrefix -split "`n") | Where-Object { $_ -ne '' }) + @($blanked)
                if ($prefixLines.Count -gt 8) { $prefixLines = $prefixLines[-8..-1] }
                $statementPrefix = $prefixLines -join "`n"
            }
        }
        elseif ($typeScriptExtensions -contains $f.Extension) {
            Scan-TypeScriptLine $repo $rel $lineNum $area $raw
            if (($Rule -contains 14) -and $area -eq 'prod' -and $raw -match '^\s*styles\s*:\s*[\[`''"]') {
                Add-Row $repo $rel $lineNum $area 14 'component-inline-styles' 'use-utilities' $raw
            }
            if (($Rule -contains 11) -and ($area -eq 'test-unit' -or $area -eq 'test-e2e')) {
                Scan-TsTestLiteralLine $repo $rel $lineNum $area $raw
            }
        }
        if (($Rule -contains 10) -and $area -eq 'test-e2e') {
            Scan-SelectorLine $repo $rel $lineNum $area $raw $f.Extension
            Scan-CopyAssertionLine $repo $rel $lineNum $area $raw
        }
        if (($Rule -contains 17) -and $area -ne 'prod') {
            Scan-TimeoutLine $repo $rel $lineNum $area $raw $f.Extension
        }
        if (($Rule -contains 11) -and $sweepsTestData -and $f.Extension -eq '.cs') {
            Scan-TestLiteralLine $repo $rel $lineNum $area $raw $prefixBefore $continuation
            Test-GeneratedValueLine $repo $rel $lineNum $area $raw
        }
    }
}

# Rule 14: authored CSS is a row per declaration, closed only by deleting it. The exemptions are the
# framework's own configuration and nothing else: @theme and daisyUI's @plugin blocks define tokens, @font-face
# loads a face, a top-level at-rule statement (@import, @plugin, @source) wires the framework, a
# prefers-color-scheme :root block may re-bind a name the file's @theme already defines (a scheme, never a new
# token and never an alias), and an @layer base rule may style type selectors only, because Preflight strips
# heading, list and link styles and no utility can reach every h2 or a. A class, id, universal selector or
# combinator in @layer base is authored styling like any other.
$cssTypeSelector = '^[a-z][a-z0-9-]*(?:\[[^\]]*\]|::?[a-z-]+(?:\([^().#*\[\]]*(?:\([^().#*\[\]]*\))?[^().#*\[\]]*\))?)*$'
$schemeMedia = '^@media\s*\(\s*prefers-color-scheme\s*:\s*(?:light|dark)\s*\)$'

function Split-CssSelectorList([string]$selector) {
    $parts = New-Object System.Collections.Generic.List[string]
    $depth = 0
    $current = New-Object System.Text.StringBuilder
    foreach ($character in $selector.ToCharArray()) {
        if ($character -eq '(' -or $character -eq '[') { $depth++ }
        elseif ($character -eq ')' -or $character -eq ']') { $depth-- }
        if ($character -eq ',' -and $depth -eq 0) {
            $parts.Add($current.ToString().Trim())
            [void]$current.Clear()
            continue
        }
        [void]$current.Append($character)
    }
    $parts.Add($current.ToString().Trim())
    return $parts
}

function Test-CssTypeSelectorList([string]$selector) {
    foreach ($part in (Split-CssSelectorList $selector)) {
        if ($part -notmatch $cssTypeSelector) { return $false }
    }
    return $true
}

function Get-CssThemeNames([string]$css) {
    $names = New-Object 'System.Collections.Generic.HashSet[string]' ([StringComparer]::Ordinal)
    foreach ($block in [regex]::Matches($css, '@theme\b[^{]*\{([^{}]*)\}')) {
        foreach ($name in [regex]::Matches($block.Groups[1].Value, '(--[\w-]+)\s*:')) { [void]$names.Add($name.Groups[1].Value) }
    }
    return $names
}

function Get-CssRuleFourteenRows([string]$css, [int]$firstLine) {
    $found = New-Object System.Collections.Generic.List[object]
    $themeNames = Get-CssThemeNames $css
    $stack = New-Object System.Collections.Generic.List[string]
    $buffer = New-Object System.Text.StringBuilder
    $line = $firstLine
    $statementLine = $firstLine
    $quote = [char]0
    $judge = {
        param([string]$statement, [int]$at)
        if ($statement -eq '') { return }
        if ($stack.Count -eq 0) {
            if (-not $statement.StartsWith('@')) { $found.Add([pscustomobject]@{ Line = $at; Category = 'authored-statement'; Disposition = 'use-utilities' }) }
            return
        }
        if ($stack | Where-Object { $_ -match '^@(?:theme|plugin|font-face)\b' }) { return }
        $selector = $stack[$stack.Count - 1]
        if ($statement.StartsWith('--')) {
            $name = ($statement -split ':', 2)[0].Trim()
            $value = if ($statement.Contains(':')) { ($statement -split ':', 2)[1].Trim() } else { '' }
            $inScheme = $stack | Where-Object { $_ -match $schemeMedia }
            if ($selector -eq ':root' -and $inScheme -and $themeNames.Contains($name) -and -not $value.StartsWith('var(')) { return }
            $found.Add([pscustomobject]@{ Line = $at; Category = 'custom-property'; Disposition = 'move-to-theme' })
            return
        }
        if (($stack -contains '@layer base') -and -not $selector.StartsWith('@') -and (Test-CssTypeSelectorList $selector)) { return }
        $category = if ($stack | Where-Object { $_ -match '^@keyframes\b' }) { 'authored-keyframes' }
        elseif ($stack | Where-Object { $_ -match '^@utility\b' }) { 'authored-utility' }
        else { 'authored-declaration' }
        $found.Add([pscustomobject]@{ Line = $at; Category = $category; Disposition = 'use-utilities' })
    }
    for ($k = 0; $k -lt $css.Length; $k++) {
        $character = $css[$k]
        if ($character -eq "`n") { $line++ }
        if ($quote -ne [char]0) {
            [void]$buffer.Append($character)
            if ($character -eq $quote) { $quote = [char]0 }
            continue
        }
        if ($character -eq '/' -and $k + 1 -lt $css.Length -and $css[$k + 1] -eq '*') {
            $close = $css.IndexOf('*/', $k + 2)
            if ($close -lt 0) { $close = $css.Length - 2 }
            $line += ([regex]::Matches($css.Substring($k, $close + 2 - $k), "`n")).Count
            $k = $close + 1
            continue
        }
        if ($character -eq '"' -or $character -eq "'") { $quote = $character }
        if ($buffer.ToString().Trim() -eq '' -and $character -notmatch '\s') { $statementLine = $line }
        if ($character -eq '{') {
            $stack.Add(($buffer.ToString().Trim() -replace '\s+', ' '))
            [void]$buffer.Clear()
        }
        elseif ($character -eq ';') {
            & $judge $buffer.ToString().Trim() $statementLine
            [void]$buffer.Clear()
        }
        elseif ($character -eq '}') {
            & $judge $buffer.ToString().Trim() $statementLine
            [void]$buffer.Clear()
            if ($stack.Count -gt 0) { $stack.RemoveAt($stack.Count - 1) }
        }
        else {
            [void]$buffer.Append($character)
        }
    }
    return $found
}

if ($Rule -contains 14) {
    $styleAttribute = '(?<![\w\[\].:@-])style\s*=\s*("[^"]*\S[^"]*"|''[^'']*\S[^'']*'')'
    foreach ($f in @($walked | Where-Object { $rule14Extensions -contains $_.Extension })) {
        if ($f.FullName -like '*\wwwroot\lib\*') { continue }
        $repo = Get-RepoName $f.FullName $WorkspaceRoot
        if ($repo.StartsWith('.')) { continue }
        $rel = $f.FullName.Substring($WorkspaceRoot.Length).TrimStart('\')
        $area = Get-Area $rel
        $text = Get-Content -LiteralPath $f.FullName -Raw -ErrorAction SilentlyContinue
        if (-not $text) { continue }
        $lines = $text -split "`r?`n"
        $blocks = if ($f.Extension -eq '.css') {
            @([pscustomobject]@{ Css = $text; FirstLine = 1 })
        }
        else {
            @([regex]::Matches($text, '(?is)<style\b[^>]*>(.*?)</style>') | ForEach-Object {
                    [pscustomobject]@{
                        Css       = $_.Groups[1].Value
                        FirstLine = 1 + ([regex]::Matches($text.Substring(0, $_.Groups[1].Index), "`n")).Count
                    }
                })
        }
        foreach ($block in $blocks) {
            foreach ($finding in (Get-CssRuleFourteenRows $block.Css $block.FirstLine)) {
                $category = if ($f.Extension -eq '.css') { $finding.Category } else { "style-block-$($finding.Category)" }
                Add-Row $repo $rel $finding.Line $area 14 $category $finding.Disposition $lines[$finding.Line - 1]
            }
        }
        if ($f.Extension -ne '.css') {
            for ($i = 0; $i -lt $lines.Count; $i++) {
                if ($lines[$i] -match $styleAttribute) {
                    Add-Row $repo $rel ($i + 1) $area 14 'style-attribute' 'use-utilities' $lines[$i]
                }
            }
        }
    }
}

$outDir = Split-Path -Parent $OutputPath
if (-not (Test-Path $outDir)) { New-Item -ItemType Directory -Path $outDir -Force | Out-Null }
$rows | Sort-Object Rule, Repo, Path, Line | Export-Csv -Path $OutputPath -NoTypeInformation -Encoding UTF8

Write-Host ""
Write-Host "Scanned $total files. Wrote $($rows.Count) rows to $OutputPath"
Write-Host ""
Write-Host "=== by rule and disposition ==="
$rows | Group-Object Rule, Disposition |
    Sort-Object { [int]($_.Name -split ',')[0].Trim() }, Name |
    Select-Object @{n = 'Rule/Disposition'; e = { $_.Name } }, Count |
    Format-Table -AutoSize
Write-Host "=== by repo ==="
$rows | Group-Object Repo | Sort-Object Count -Descending |
    Select-Object Name, Count | Format-Table -AutoSize

Write-Host "=== IN SCOPE BUT NOT SCANNED - these languages have no rule scanner here ==="
Write-Host "Zero rows for a language below means no detector ran, NOT that the code complies."
if ($unscanned.Count -eq 0) {
    Write-Host "  (none - every in-scope file was scanned)"
}
else {
    $unscanned | Group-Object { $extMap[$_.Extension] } | Sort-Object Count -Descending |
        Select-Object @{n = 'Language'; e = { $_.Name } }, Count | Format-Table -AutoSize
    Write-Host "  $($unscanned.Count) of $($inScope.Count) in-scope files were skipped for lack of a scanner."
}
if ($Rule -contains 11) {
    Write-Host "  Exception: rule 11's copied-production-text check also reads Python (.py) test and production code."
}
if ($Rule -contains 12) {
    Write-Host "  Exception: rule 12 also reads Python (.py) tests for logic in a test_ function body."
}
if ($Rule -contains 14) {
    Write-Host "  Exception: rule 14 reads every .css file, and the <style> blocks and style attributes of .html, .cshtml, .razor and .astro files."
}

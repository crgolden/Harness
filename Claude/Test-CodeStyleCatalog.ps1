# GATE-STEP: Code-style catalog harness
# GATE-LISTING: -Path $fixtureRoot | none
<#
.SYNOPSIS
    Regression harness for Get-CodeStyleCatalog.ps1, in the same spirit as the hook suites
    test_stop_gate.py and test_pretooluse_gate.py.

.DESCRIPTION
    Builds a synthetic fixture workspace in a temp directory, runs Get-CodeStyleCatalog.ps1 against it,
    and asserts that each rule fires on the lines it should and stays silent on the lines it must not.

    It then guards the other half of the same machinery: that the caveats in Tools/AGENTS/CODE-STYLE.md which
    explain WHY each detector behaves as it does are still in the doc. A detector and its written
    justification fail independently - a compression pass can trim the prose while every assertion here
    still passes - and whoever edits a detector is exactly who might trim its explanation.

    **Fixtures, not the live workspace, on purpose.** Asserting "rule 5 returns exactly 10 rows" against
    the real repos was how these controls started, and it rots the moment anyone edits a test - the file
    count moved from 1054 to 1074 during the session that wrote this script. A fixture workspace makes the
    assertions deterministic and lets each one name the exact behaviour it protects.

    Every MUST-NOT case here is a bug that was actually shipped and caught during development, or a
    documented exception that a naive regex gets wrong:

      rule 5  - `redisValue.IsNullOrEmpty` is StackExchange.Redis' RedisValue property, not
                string.IsNullOrEmpty. Flagging it sends someone to "fix" working code.
      rule 9  - `a != b` must not read as a null-forgiving `!`.
      rule 12 - branching inside a named helper is explicitly allowed by TESTING.md; only [Fact]/[Theory]
                bodies count. The first implementation tracked brace depth from the method signature
                line, which carries no `{` under Allman bracing, so every test body was skipped and the
                rule reported zero against a real population of 61.
      rule 11 - xUnit plumbing (`[Trait]`, `[Fact(DisplayName=...)]`) is not test data; an early version
                emitted 315 keep-kat rows that were mostly `[Trait("Category", "Unit")]`.
      rule 11 - a well-named local (`ownerId`) is the CORRECT form and must not be flagged; only
                placeholder names (`id`, `guid1`) and inlined generators are.
      rule 10 - `input[name='Input.Email']` is rule 10's documented exception and must not be flagged;
                third-party DOM (Leaflet) must be separated from ordinary class selectors, not lumped in.
      rule 8  - the char overloads (`StartsWith('{')`, `IndexOf('?')`) are ORDINAL already and correct as
                written; only the string overloads are culture-sensitive. Flagging a char overload sends
                someone to "fix" working code, and the only visible difference is the quote character.
      rule 5  - a `//` inside a string literal is not a comment. `Remove-LineComment` was a plain
                `'//.*$'` replace, so `url.StartsWith("http://", ...)` was truncated at the `//` INSIDE
                the literal and every rule went blind to the rest of the line. The rule-5 assertion on
                `CommentStripperRegression` is that fix's control - it fails on the old stripper.

.EXAMPLE
    .\Test-CodeStyleCatalog.ps1

.EXAMPLE
    .\Test-CodeStyleCatalog.ps1 -KeepFixtures
#>
[CmdletBinding()]
param(
    [switch]$KeepFixtures
)

$ErrorActionPreference = 'Stop'

$scriptUnderTest = Join-Path $PSScriptRoot 'Get-CodeStyleCatalog.ps1'
if (-not (Test-Path -LiteralPath $scriptUnderTest)) {
    throw "Get-CodeStyleCatalog.ps1 not found next to this harness at '$scriptUnderTest'."
}

. "$PSScriptRoot\..\Hooks\ClaudeTemp.ps1"

$fixtureRoot = New-ClaudeTempPath "codestyle-fixtures-$([guid]::NewGuid().ToString('N').Substring(0, 8))"
$csvPath = Join-Path $fixtureRoot 'out.csv'

$prodDir = Join-Path $fixtureRoot 'FixtureApp\FixtureApp'
$unitDir = Join-Path $fixtureRoot 'FixtureApp\FixtureApp.Tests.Unit'
$e2eDir = Join-Path $fixtureRoot 'FixtureApp\e2e'
foreach ($d in @($prodDir, $unitDir, $e2eDir)) { New-Item -ItemType Directory -Path $d -Force | Out-Null }

$prodCode = @'
namespace FixtureApp;

public class ProdFixture
{
    public string? Name { get; set; } = string.Empty;
    public List<string>? Tags { get; set; }
    public string[]? Aliases { get; set; }
    public byte[]? EncryptedKey { get; set; }
    public DateTime CreatedAt { get; set; }
    public string? ManualUrl { get; set; }
    public Uri? SchemaDeclaredUri { get; set; }
    public string? ParsedRunId { get; init; }
    public string? ParsedReleaseStamp { get; init; }
    public string? OpaqueEntitlementId { get; init; }
    public string? OpaqueConceptType { get; init; }
    public string? LocallyParsedToken { get; init; }

    public Guid ReadRun(ProdFixture envelope) => Guid.Parse(envelope.ParsedRunId);

    public DateTimeOffset Released(ProdFixture? envelope) => DateTimeOffset.Parse(envelope?.ParsedReleaseStamp, CultureInfo.InvariantCulture);

    public string? BoundaryStamp { get; init; }

    public bool IsStamped(ProdFixture envelope) => DateTimeOffset.TryParse(envelope.BoundaryStamp, out _);

    public decimal? ReadCsvLatitude(string? csvLatitude) => decimal.TryParse(csvLatitude, out var latitude) ? latitude : null;

    public bool IsToken(string locallyParsedToken) => Guid.TryParse(locallyParsedToken, out _);

    public Guid RecordOwner(string ownerSub) => Guid.Parse(ownerSub);

    public int MeasureHandle(string opaqueHandle) => opaqueHandle.Length;

    public int LargestFlag() => Enum.GetValues<DayOfWeek>().Max(flagValue => Convert.ToInt32(flagValue, CultureInfo.InvariantCulture));

    public string PadFlag(string flagValue) => $" {flagValue} ";

    public Guid ReadCount(string countText) => Guid.Parse(countText);

    public int CountAll(string[] texts) => texts.Count(countText => Guid.Parse(countText) != Guid.Empty);

    public static Guid FromCell(object cell) => cell switch { string cellText => Guid.Parse(cellText), _ => Guid.Empty };

    public Uri BuildEndpoint(Uri baseAddress)
    {
        var composed = new Uri(baseAddress, "relative");
        return composed;
    }

    public string? PhoneNumber { get; set; }

    public ProdFixture Rehydrate() =>
        new ProdFixture
        {
            PhoneNumber = null,
        };

    public string Coalesced(string? input) => input ?? string.Empty;

    public NeedStub DefaultNeed() => NeedStub.EveryProvider(string.Empty);

    public void AddPlaceholderRole(List<string> roleRows) => roleRows.Add(string.Empty);

    public string WithoutSpaces(string spaced) => spaced.Replace(" ", string.Empty, StringComparison.Ordinal);

    public string GluedParts(string[] partsToGlue) => string.Join(string.Empty, partsToGlue);

    public void ReportModelLevelFailure(ModelStateStub modelState, string failureText) =>
        modelState.AddModelError(string.Empty, failureText);

    public void ReportBlankFailureText(ModelStateStub modelState, string propertyName) =>
        modelState.AddModelError(propertyName, string.Empty);

    public string Bracketed(string bracketValue) => Wrap("(x)", bracketValue);

    public string Blank() => string.Empty;

    public string BlankReturn()
    {
        return string.Empty;
    }

    public string BlankLiteralReturn()
    {
        return "";
    }

    public string BareEmptyAssigned { get; set; } = Empty;

    public string BareEmptyReturn()
    {
        return Empty;
    }

    public string BareEmptyCoalesced(string? input) => input ?? Empty;

    public string[] NotAnEmptyString() => Array.Empty<string>();

    public ImmutableArray<int> AlsoNotAnEmptyString() => ImmutableArray<int>.Empty;

    public string EmptyLabelIsNotTheKeyword() => EmptyLabel;

    public bool WeakCheck(string? value) => string.IsNullOrEmpty(value);

    public bool RedisStyleCheck(RedisValueStub cached) => cached.IsNullOrEmpty;

    public bool NotEqualIsNotNullForgiving(int a, int b) => a != b;

    public string NullForgiving(string? maybe) => maybe!.Trim();

    public string CallResultNullForgiving(IDictionary<string, string> map) => map.GetValueOrDefault("key")!.Trim();

    public string IndexResultNullForgiving(string?[] items) => items[0]!.Trim();

    public bool NotEqualOnCallResult(string a, string b) => a.GetHashCode() != b.GetHashCode();

    public bool LiteralEquality(string kind) => kind == "denomination";

    public string Banner(string name) => $"=== {name} ===";

    public const string Base64Vector = "QUJDRA==";

    public bool SchemeCheck(string url) => url.StartsWith("http://", StringComparison.OrdinalIgnoreCase);

    public bool BracePrefix(string body) => body.StartsWith('{');

    public bool ExplicitEquals(string a, string b) => string.Equals(a, b, StringComparison.Ordinal);

    public IEnumerable<string> SortedExplicitly(IEnumerable<string> names) => names.OrderBy(n => n, StringComparer.Ordinal);

    public bool CommentStripperRegression(string url) => url.StartsWith("http://", StringComparison.Ordinal) && string.IsNullOrEmpty(url);

    public bool BearerPrefix(string header) => header.StartsWith("Bearer ");

    public bool MixedCompliance(string header, string a, string b) => header.StartsWith("Bearer ") && string.Equals(a, b, StringComparison.Ordinal);

    public bool SameClaim(string a, string b) => string.Equals(a, b);

    public bool HasScheme(string url) => url.Contains("://");

    public void ConfigureClient(HttpClient httpClient) => httpClient.Timeout = TimeSpan.FromSeconds(30);

    public bool ParameterBound(DbParameterCollection parameters) => parameters.Contains("@Slug");

    public void CollectionMembership(IEnumerable<string> names) => Assert.Contains("openid", names);

    public int Ranked(string a, string b) => a.CompareTo(b);

    public string Folded(string value) => value.ToLower();

    public IEnumerable<string> SortedLoosely(IEnumerable<string> names) => names.OrderBy(n => n);

    public bool WrappedOrdinal(string a, string b) => string.Equals(
        a,
        b,
        StringComparison.Ordinal);

    public bool WrappedWithoutComparison(string a, string b) => string.Equals(
        a,
        b);

    public async Task<bool> EfLiteralPredicate(AppDbContextStub db) =>
        await db.Resources.AnyAsync(r => r.Name == "openid");

    public async Task<List<ClientStub>> EfOrdering(AppDbContextStub _context) =>
        await _context.Clients.OrderBy(c => c.ClientId).ToListAsync();

    public async Task<List<ClientStub>> EfOrderingWrapped(AppDbContextStub _context) =>
        await _context.Clients
            .OrderByDescending(c => c.Created)
            .ToListAsync();

    public IQueryable<UserStub> IdentityManagerOrdering(UserManagerStub _userManager) =>
        _userManager.Users.OrderBy(u => u.UserName);

    public IEnumerable<ClientStub> NewestFirst(IEnumerable<ClientStub> clients) =>
        clients.OrderByDescending(c => c.CreatedAt).ThenBy(c => c.MsrpPrice);

    public Dictionary<string, int> KeyedWithEqualityComparer(IEnumerable<string> names) => names.ToDictionary(
        name => name,
        name => name.Length,
        EqualityComparer<string>.Default);

    public Dictionary<string, int> KeyedWithoutComparer(IEnumerable<string> names) => names.ToDictionary(
        name => name,
        name => name.Length);

    public IEnumerable<RuleStub> RankedWithIntComparer(IEnumerable<RuleStub> rules) => rules.OrderBy(rule => rule.Rank, Comparer<int>.Default);

    public IEnumerable<RuleStub> RankedWithStringComparer(IEnumerable<RuleStub> rules) => rules.OrderBy(rule => rule.Label, Comparer<string>.Default);

    public IEnumerable<RuleStub> RankedWithQualifiedStringComparer(IEnumerable<RuleStub> rules) => rules.OrderBy(rule => rule.Label, Comparer<System.String>.Default);

    public void ConfigureQueryOptions(ODataOptionsStub oDataOptions)
    {
        oDataOptions.Select().Filter().OrderBy();
    }

    public void BuildEdm(ODataModelBuilderStub modelBuilder)
    {
        modelBuilder.Ignore<EdmIgnoredRequestFixture>();
    }

    public void OrdinaryLoggerFactoryDependency(ILoggerFactory loggerFactory)
    {
    }

    public void Narrate() => Console.WriteLine("starting up");

    public void Stamp() => Console.WriteLine(DateTime.UtcNow);
}
'@

$unitCode = @'
namespace FixtureApp.Tests.Unit;

public class FixtureTests
{
    [Trait("Category", "Unit")]
    [Fact]
    public void WellNamedLocal_IsNotFlagged()
    {
        var ownerId = Guid.NewGuid();
        Assert.NotEqual(Guid.Empty, ownerId);
    }

    [Given("a member with a confirmed account")]
    public void StepBindingText_IsNotFlagged()
    {
    }

    [GivenData("a lookalike step attribute")]
    public void LookalikeStepAttribute_IsFlagged()
    {
    }

    [InlineData("a theory row specimen")]
    public void InlineDataSpecimen_IsFlagged(string specimen)
    {
    }

    [Fact]
    public void PlaceholderLocal_IsFlagged()
    {
        var id = Guid.NewGuid();
        Assert.NotEqual(Guid.Empty, id);
    }

    [Fact]
    public void InlinedGenerator_IsFlagged()
    {
        var result = Lookup(Guid.NewGuid());
        Assert.Null(result);
    }

    [Fact]
    public void InterpolatedGeneratorInNamedLocal_IsNotFlagged()
    {
        var configuredKey = $"value{Guid.NewGuid():N}";
        Assert.NotNull(configuredKey);
    }

    [Fact]
    public void RawStringBlock_IsNotSweptForMagicNumbers()
    {
        const string insertSql = """
            INSERT INTO church (id, name, state, zip)
            VALUES ($1, $2, $3, $4)
            """;
        Assert.NotNull(insertSql);
    }

    [Fact]
    public void RawPayloadBlock_IsReportedOnce()
    {
        const string payloadBody = """
            {"lambda": 4077}
            """;
        Assert.NotNull(payloadBody);
    }

    [Fact]
    public void SelfContainedRawString_IsAPayloadJudgedAsAUnit()
    {
        var seeded = Deserialize("""{"id": 201930, "kind": "baptist"}""");
        Assert.NotNull(seeded);
    }

    [Fact]
    public void LiteralBesideASelfContainedRawString_IsStillFlagged()
    {
        var described = string.Concat(Deserialize("""{"id": 4071}"""), "denomination");
        Assert.NotNull(described);
    }

    [Fact]
    public void MagicStringLocal_IsFlagged()
    {
        var label = "denomination";
        Assert.NotNull(label);
    }

    [Fact]
    public void BareDistinct_IsReportedForTriage()
    {
        var kinds = Seed().Distinct();
        Assert.Empty(kinds);
    }

    [Fact]
    public void ComparerSuppliedDistinct_IsNotFlagged()
    {
        var kinds = Seed().Distinct(EqualityComparer<string>.Default);
        Assert.Empty(kinds);
    }

    [Fact]
    public void ExceptionMessageLiteral_IsNotTestData()
    {
        var failure = new InvalidOperationException("the stub was never primed");
        Assert.NotNull(failure);
    }

    [Fact]
    public void DataTableColumnLabel_IsNotTestData()
    {
        var table = new DataTable();
        table.Columns.Add("Slug", typeof(string));
        Assert.Single(table.Columns);
    }

    [Theory]
    [InlineData("({0}) {1}-{2}")]
    public void CompositeFormatTemplate_IsNotTestData(string template)
    {
        Assert.NotNull(template);
    }

    [Fact]
    public void PunctuationOnlyInterpolation_IsNotTestData()
    {
        var scheme = Seed().First();
        var token = Seed().Last();
        var header = $"{scheme} {token}";
        Assert.NotNull(header);
    }

    [Fact]
    public void WordedInterpolation_IsFlagged()
    {
        var token = Seed().First();
        var header = $"Bearer {token}";
        Assert.NotNull(header);
    }

    [Fact]
    public void PunctuationOnlyConcatenation_IsNotTestData()
    {
        var keyword = Seed().First();
        Assert.Equal(keyword + "%", Capture().Parameters["@Slug"].Value);
    }

    [Fact]
    public void WordedConcatenation_IsFlagged()
    {
        var keyword = Seed().First();
        Assert.Equal(keyword + " AND ", Capture().Parameters["@Slug"].Value);
    }

    [Fact]
    public void ParameterIndexerName_IsTheSqlContract()
    {
        var insert = Capture();
        var bound = insert.Parameters["@Slug"].Value;
        Assert.NotNull(bound);
    }

    [Fact]
    public void SqlTextAssertion_IsTheThingUnderTest()
    {
        var sql = Capture();
        Assert.Contains("INSERT INTO church", sql, StringComparison.Ordinal);
        Assert.True(sql.Contains("[dbo].[Churches]", StringComparison.Ordinal), "the writer targets the churches table");
    }

    [Fact]
    public void SqlTextAssertedThroughAnIndexer_IsStillTheThingUnderTest()
    {
        var executed = CaptureAll();
        var insertStatement = InsertStatementIndex();
        Assert.Contains("INSERT INTO job_runs", executed[insertStatement].CommandText, StringComparison.Ordinal);
    }

    [Fact]
    public void InterpolatedSqlAssertedAgainstTheStatement_IsStillTheThingUnderTest()
    {
        var keyword = Seed().First();
        var containsSql = Capture().CommandText;
        Assert.Equal($"CONTAINS(name, '{keyword}*') AND active = 1", containsSql);
    }

    [Fact]
    public void InterpolatedTextAssertedAgainstANonSqlReceiver_IsFlagged()
    {
        var keyword = Seed().First();
        var label = Capture().Label;
        Assert.Equal($"denomination {keyword} AND parish", label);
    }

    [Fact]
    public void MagicStringAssertedThroughAnIndexerOnANonSqlReceiver_IsFlagged()
    {
        var executed = CaptureAll();
        var firstRow = FirstRowIndex();
        Assert.Contains("baptist", executed[firstRow].Denomination, StringComparison.Ordinal);
    }

    [Fact]
    public void GeneratorBounds_AreTheGeneratorsDomain()
    {
        var attemptCount = Random.Shared.Next(1, 101);
        Assert.NotEqual(0, attemptCount);
    }

    [Fact]
    public void MagicNumberBesideGeneratorBounds_IsFlagged()
    {
        var pageOffset = Random.Shared.Next(1, 101) + 250;
        Assert.NotEqual(0, pageOffset);
    }

    [Fact]
    public void UsingDeclaration_IsAnOrdinaryLocal()
    {
        using var expiry = new CancellationTokenSource(TimeSpan.FromMilliseconds(300));
        Assert.NotNull(expiry);
    }

    [Fact]
    public void SharedGeneratorArgument_IsTheGeneratorsDomain()
    {
        var denominationWord = Generated.LowercaseToken(6);
        Assert.NotNull(denominationWord);
    }

    [Fact]
    public void MagicNumberBesideASharedGeneratorArgument_IsFlagged()
    {
        var pageOffset = Generated.LowercaseToken(6).Length + 250;
        Assert.NotEqual(0, pageOffset);
    }

    [Fact]
    public void PerProjectGeneratorClassArgument_IsStillFlagged()
    {
        var localWord = TestValues.LowercaseToken(7);
        Assert.NotNull(localWord);
    }

    [Fact]
    public void ArgumentToAReceiverThatIsNotAGenerator_IsStillFlagged()
    {
        var unrelatedWord = Elsewhere.LowercaseToken(9);
        Assert.NotNull(unrelatedWord);
    }

    [Fact]
    public void GeneratorScaledIntoItsDomain_IsNamedByTheLocal()
    {
        var starRating = Random.Shared.Next(0, 10) / HalfStarStep;
        Assert.NotEqual(0, starRating);
    }

    [Fact]
    public void GeneratorPassedAsAnArgument_IsStillUnnamed()
    {
        var accepted = Sink.Accept(Guid.NewGuid());
        Assert.NotEqual(0, accepted);
    }

    [Fact]
    public void AssertionFailureMessage_IsNotTestData()
    {
        Assert.True(Seed().Any(), "the seed must not be empty");
    }

    [Fact]
    public void WrappedExceptionMessage_IsNotTestData()
    {
        var failure = new InvalidOperationException(
            "the enumerator always throws, so Current is never reachable");
        Assert.NotNull(failure);
    }

    [Fact]
    public void InterpolatedExceptionMessage_IsNotTestData()
    {
        var failure = new InvalidOperationException($"{nameof(Seed)} was never primed");
        Assert.NotNull(failure);
    }

    [Fact]
    public void FormatSpecifier_IsNotTestData()
    {
        var sessionKey = Guid.NewGuid().ToString("N");
        Assert.NotNull(sessionKey);
    }

    [Fact]
    public void SuffixedZero_IsABoundary()
    {
        var total = Seed().Count();
        Assert.Equal(0m, total);
    }

    [Fact]
    public void SuffixedMagicNumber_IsFlagged()
    {
        var total = Seed().Count();
        Assert.Equal(5m, total);
    }

    [Fact]
    public void ZeroFractionBoundary_IsABoundary()
    {
        var average = Seed().Average(church => church.Score);
        Assert.Equal(1.0, average);
    }

    [Fact]
    public void ZeroFractionMagicNumber_IsFlagged()
    {
        var average = Seed().Average(church => church.Score);
        Assert.Equal(20.0, average);
    }

    [Fact]
    public void FractionStartingWithZero_IsFlagged()
    {
        var average = Seed().Average(church => church.Score);
        Assert.Equal(0.05, average);
    }

    [Fact]
    public void PinnedProductionConstant_AssertsTheLiteral()
    {
        Assert.Equal("identity.consent.granted", Telemetry.Metrics.ConsentGrantedCounterName);
    }

    [Fact]
    public void ResultComparedToALiteral_IsFlagged()
    {
        var church = Seed().First();
        Assert.Equal("baptist", church.Denomination);
    }

    [Fact]
    public void ReassignedNamedLocal_IsNotInline()
    {
        string traceIdentifier;
        traceIdentifier = $"trace-{Guid.NewGuid():N}";
        Assert.NotNull(traceIdentifier);
    }

    internal static Guid NewOwnerId() => Guid.NewGuid();

    private static FixtureChurch NewDetailedChurchFixture(string city)
    {
        return new FixtureChurch(city, city);
    }

    private static int NewAttemptBudget()
    {
        return Random.Shared.Next(1, 10);
    }

    private static byte[] NewSigningKey()
    {
        var key = new byte[KeyLength];
        RandomNumberGenerator.Fill(key);
        return key;
    }

    private static string NewRegionName() => Generated.NewCity();

    private static FixtureChurch NewChurchFixture(string city) => new(city, Generated.NewZip());

    private static readonly Guid SharedFixtureChatId = Guid.NewGuid();

    [Fact]
    public void ExceptionMessageCopy_RestatesProductionText()
    {
        var missingKey = Seed().First();
        var failure = Record.Exception(() => Lookup(missingKey));
        Assert.Equal($"Invalid '{missingKey}'.", failure.Message);
        Assert.Equal("key", ((ArgumentException)failure).ParamName);
    }

    [Fact]
    public void RootPath_IsABoundary()
    {
        Assert.True(Seed().Contains("/"));
    }

    [Fact]
    public void PinnedTestOwnedConstant_IsAlsoAPin()
    {
        Assert.Equal("Unit", UnitCollection.Name);
    }

    [Fact]
    public void PinnedProductionCollection_AssertsTheSpecNames()
    {
        Assert.Equal(
            ["$filter", "$select", "$top"],
            Telemetry.Metrics.SystemQueryOptions);
    }

    [Fact]
    public void CollectionComparedToALocal_IsFlagged()
    {
        var actualOptions = Seed().ToArray();
        Assert.Equal(
            ["gamma", "delta"],
            actualOptions);
    }

    [Fact]
    public void PinnedProductionConstant_ReadUnderAct_AssertsTheLiteral()
    {
        // Act
        var counterName = Telemetry.Metrics.ConsentDeniedCounterName;

        // Assert
        Assert.Equal("identity.consent.denied", counterName);
    }

    [Fact]
    public void PinnedProductionPair_BuiltUnderAct_AssertsTheLiterals()
    {
        // Act
        string[] claim = [Telemetry.Metrics.ModeratorClaimType, Telemetry.Metrics.ModeratorClaimValue];

        // Assert
        Assert.Equal(["churches.mod", "true"], claim);
    }

    [Fact]
    public void PinnedProductionCollection_AgainstAnExpectedLocal_AssertsTheLiterals()
    {
        // Arrange
        string[] expected =
        [
            "identity.grants.revoked",
            "identity.exceptions",
        ];

        // Act
        string[] actual =
        [
            Telemetry.Metrics.GrantsRevokedCounterName,
            Telemetry.Metrics.ExceptionCounterName,
        ];

        // Assert
        Assert.Equal(expected, actual);
    }

    [Fact]
    public void PinnedProductionNumbers_BuiltUnderAct_AssertTheLiterals()
    {
        // Act
        int[] budget = [Telemetry.Metrics.RequestsPerMinute, Telemetry.Metrics.TokensPerMinute];

        // Assert
        Assert.Equal([371, 371_000], budget);
    }

    [Fact]
    public void ComputedNumberReadUnderAct_ComparedToALiteral_IsFlagged()
    {
        // Act
        var computedBudget = Seed().Count();

        // Assert
        Assert.Equal(373, computedBudget);
    }

    [Fact]
    public void ComputedResultReadUnderAct_ComparedToALiteral_IsFlagged()
    {
        // Act
        var computedName = Telemetry.Metrics.Describe();

        // Assert
        Assert.Equal("identity.described", computedName);
    }

    [Fact]
    public void LocalReusingAPinnedName_FromAnotherTest_IsFlagged()
    {
        // Act
        var counterName = Seed().First();

        // Assert
        Assert.Equal("identity.unpinned", counterName);
    }

    [Fact]
    public void ExpectedLocal_AgainstAComputedCollection_IsFlagged()
    {
        // Arrange
        string[] expected = ["epsilon", "zeta"];

        // Act
        var actual = Seed().ToArray();

        // Assert
        Assert.Equal(expected, actual);
    }

    [Fact]
    public void LowercaseWordEndingInHost_IsOrdinaryTestData()
    {
        var loopbackHost = "localhost";
        Assert.NotNull(loopbackHost);
    }

    [Fact]
    public void PascalCaseConfigurationKey_IsAProductionIdentifier()
    {
        var configuredHost = "MongoDbHost";
        Assert.NotNull(configuredHost);
    }

    [Fact]
    public void ParameterMembership_IsTheSqlContract()
    {
        var insert = Capture();
        Assert.True(insert.Parameters.Contains("@DayOfWeek"));
    }

    [Fact]
    public void NonParameterMembership_IsStillTestData()
    {
        var insert = Capture();
        Assert.True(insert.Tags.Contains("denomination"));
    }

    [Fact]
    public void MagicStringBesideAParametersIndexer_IsFlagged()
    {
        var insert = Capture();
        Assert.Equal("denomination", insert.Parameters["@Kind"].Value);
    }

    [Fact]
    public void SqlTextAssertedAgainstAnotherReceiver_IsFlagged()
    {
        var commandLog = Capture();
        Assert.Contains("INSERT INTO church", commandLog, StringComparison.Ordinal);
    }

    [Fact]
    public void MagicStringInsideAnAssertCondition_IsFlagged()
    {
        var church = Seed().First();
        Assert.True(church.Contains("baptist", StringComparison.Ordinal), "the seed must carry a denomination");
    }

    [Fact]
    public void WrappedAssertMessage_IsNotTestData()
    {
        Assert.False(
            Seed().Any(),
            "an empty seed is the case under test, not a datum to generate.");
    }

    [Fact]
    public void RegexPatternIsThePropertyUnderTest_NotTestData()
    {
        var upperCaseProperty = Seed().First();
        var addressProperty = Seed().Last();
        Assert.Matches("[A-Z]", upperCaseProperty);
        Assert.DoesNotMatch(@"^[a-m]+@[a-m]+\.invalid$", addressProperty);
        Assert.Matches(@"\d{4}", "pinned-actual-specimen");
    }

    [Fact]
    public void ConcatenatedWrappedAssertMessage_IsNotTestData()
    {
        var reach = Seed().Count;
        Assert.True(
            reach > 0,
            "the seeded page did not overflow the viewport, so this fixture cannot tell a "
                + $"restored position from a reset one (reach {reach})");
    }

    [Fact]
    public void MagicStringWrappedInsideAnAssertCondition_IsFlagged()
    {
        Assert.True(
            Seed().Contains(
                "wrapped-condition-vector"
                    + "-suffix"),
            "the diagnostic that names the failure");
    }

    private static InvalidOperationException NotCalled() => new("this collaborator must not be called");

    [Fact]
    public void ExceptionSubclassMessage_IsNotTestData()
    {
        var failure = new FixtureDbException(isTransient: true);
        Assert.NotNull(failure);
    }

    [Theory]
    [InlineData(true)]
    public void BranchingInTestBody_IsFlagged(bool exists)
    {
        if (exists)
        {
            Assert.True(exists);
        }
        else
        {
            Assert.False(exists);
        }
    }

    [Fact(Timeout = 5000)]
    public async Task DeadlinesAreNotSynchronisation()
    {
        using var deadlineSource = new CancellationTokenSource();
        deadlineSource.CancelAfter(TimeSpan.FromSeconds(1));
        var work = Task.CompletedTask;
        await work.WaitAsync(TimeSpan.FromSeconds(5));
        await work.WaitAsync(TestContext.Current.CancellationToken);
        await Task.Delay(TimeSpan.FromMinutes(1), fakeTimeProvider, TestContext.Current.CancellationToken);
    }

    private static string? Lookup(Guid key)
    {
        foreach (var candidate in Seed())
        {
            if (candidate == key.ToString())
            {
                return candidate;
            }
        }

        return null;
    }

    [Fact]
    public void AaaMarkersPresent_SeparatesTheThreePhases()
    {
        // Arrange
        var expectedSlug = Guid.NewGuid().ToString();

        // Act
        var actualSlug = Echo(expectedSlug);

        // Assert
        Assert.Equal(expectedSlug, actualSlug);
    }

    [Fact]
    public void AaaEmptyArrange_LabelsNothingAtAll()
    {
        // Arrange

        // Act
        var emptiedSlug = Echo(Guid.NewGuid().ToString());

        // Assert
        Assert.NotNull(emptiedSlug);
    }

    [Fact]
    public void AaaDuplicateAssert_StampsOneMarkerPerAssertionGroup()
    {
        // Arrange
        var stampedSlug = Guid.NewGuid().ToString();

        // Act
        var stampedResult = Echo(stampedSlug);

        // Assert
        Assert.NotNull(stampedResult);

        // Assert
        Assert.Equal(stampedSlug, stampedResult);
    }

    [Fact]
    public void AaaOutOfOrder_AssertsAPreconditionBeforeTheAct()
    {
        // Arrange
        var precheckedSlug = Guid.NewGuid().ToString();

        // Assert
        Assert.NotNull(precheckedSlug);

        // Act
        var precheckedResult = Echo(precheckedSlug);
    }

    [Fact]
    public void AaaPropertyBased_MarksInsideTheSampleLambda()
    {
        // Arrange
        var sampledSlugs = Gen.String[1, 8];

        sampledSlugs.Sample(sampledSlug =>
        {
            // Arrange
            var sampledExpectation = sampledSlug;

            // Act
            var sampledResult = Echo(sampledSlug);

            // Assert
            Assert.Equal(sampledExpectation, sampledResult);
        });
    }

    [Fact]
    public void AaaSiblingLambdas_EachCarryTheirOwnAssert()
    {
        // Arrange
        var siblingSlugs = Gen.String[1, 8];

        siblingSlugs.Sample(siblingSlug =>
        {
            // Act
            var firstSiblingResult = Echo(siblingSlug);

            // Assert
            Assert.NotNull(firstSiblingResult);
        });

        siblingSlugs.Sample(siblingSlug =>
        {
            // Act
            var secondSiblingResult = Echo(siblingSlug);

            // Assert
            Assert.NotNull(secondSiblingResult);
        });
    }

    [Fact]
    public void AaaMarkersAbsent_FusesActIntoTheAssertion()
    {
        var fusedSlug = Guid.NewGuid().ToString();
        Assert.Equal(fusedSlug, Echo(fusedSlug));
    }

    [Fact]
    public void AaaMarkersCombined_LabelsActAndAssertTogether()
    {
        // Arrange
        var combinedSlug = Guid.NewGuid().ToString();

        // Act & Assert
        Assert.Equal(combinedSlug, Echo(combinedSlug));
    }

    [Fact]
    public void ExpressionBodiedTest_HasNoBodyToDivide() => Assert.NotNull(Echo(Guid.NewGuid().ToString()));

    [Theory]
    [InlineData(true)]
    [InlineData(false)]
    public void AaaWrappedSignature_WithMarkers_IsCompliant(
        bool wrappedFlag)
    {
        // Arrange
        var mapped = Chain(
            v => Echo(v))
            .Trim();

        // Act
        var observedFlag = wrappedFlag;

        // Assert
        Assert.NotNull(mapped);
        Assert.Equal(wrappedFlag, observedFlag);
    }

    [Theory]
    [InlineData(true)]
    [InlineData(false)]
    public void AaaWrappedSignature_WithoutMarkers_IsReported(
        bool bareFlag)
    {
        var bareMapped = Chain(
            v => Echo(v))
            .Trim();
        Assert.Equal(bareFlag, bareMapped is not null && bareFlag);
    }

    [Theory]
    [InlineData(true)]
    [InlineData(false)]
    public void WrappedSignature_WithBranching_IsStillLogicInATest(
        bool branchFlag)
    {
        var branchObserved = Echo(Guid.NewGuid().ToString());
        if (branchFlag)
        {
            Assert.NotNull(branchObserved);
        }
    }

    private const string StatusSql =
        "SELECT status FROM job_runs WHERE run_id = $1";

    private const string PullIdSql = $"SELECT pull_id {StatusSql}";

    private const string MislabelledSql = "sigma tau";

    private const string PlainQueryText = "SELECT upsilon FROM phi_table";

    [Fact]
    public void InterpolatedSqlLocal_IsAStatementNotASpecimen()
    {
        // Arrange
        var columnName = Generated.NewColumnName();
        var nestedSql = $"SELECT {columnName} FROM tau_table WHERE id = $1";
        var nestedRows = $"SELECT omega FROM upsilon_table WHERE id = $1";
        var mislabelledLocalSql = $"kappa {columnName} lambda";

        // Act
        var read = Query(nestedSql);

        // Assert
        Assert.NotNull(read);
        Assert.NotNull(nestedRows);
        Assert.NotNull(mislabelledLocalSql);
    }

    [Fact]
    public void NestedInterpolation_IsNotTwoFragments()
    {
        // Arrange
        var innerValue = Generated.NewLabel();
        var markup = $"{Wrap(MarkupNames.Cell, $"  {innerValue}  ")}{Wrap(MarkupNames.Row, innerValue)}";
        var besideTheNesting = $"{Wrap(MarkupNames.Cell, $" {innerValue} ")}iota beside";

        // Assert
        Assert.NotNull(markup);
        Assert.NotNull(besideTheNesting);
    }

    [Fact]
    public void ExceptionMessageHoldingItsOwnLiteral_IsALabel()
    {
        // Act
        var thrown = Record.Exception(() =>
            throw new InvalidOperationException($"Expected {typeof(int).Name}, got {Seed().FirstOrDefault() ?? "null"} from: {StatusSql}"));
        var describedOutsideAnException = $"Expected {typeof(int).Name}, got {Seed().FirstOrDefault() ?? "nu text"} now";

        // Assert
        Assert.NotNull(thrown);
        Assert.NotNull(describedOutsideAnException);
    }

    [Fact]
    public void ReasonConstant_PassedAsTheAssertionMessage_IsNotData()
    {
        // Act
        var accepted = Seed().Any();

        // Assert
        const string reason =
            "omicron explains why "
            + "the pass must resume.";

        Assert.True(accepted, reason);
    }

    [Fact]
    public void ConstantComparedAsAValue_IsFlagged()
    {
        // Act
        const string expectedLabel = "pi label";
        var actualLabel = Seed().First();

        // Assert
        Assert.Equal(expectedLabel, actualLabel);
    }

    [Fact]
    public void ReasonNamedConstant_ComparedAsAValue_IsFlagged()
    {
        // Assert
        const string reason = "rho reason";
        Assert.Equal(reason, Seed().First());
    }

    [Fact]
    public void SqlFragment_ThroughASqlHelper_IsNotData()
    {
        // Assert
        Assert.Contains(Commands(), Executed("INSERT INTO chi_table"));
        Assert.Equal(Only(Commands(), "UPDATE psi_table SET").Name, Seed().First());
    }

    [Fact]
    public void LiteralThroughANonSqlHelper_IsFlagged()
    {
        // Assert
        Assert.Contains(Commands(), Labelled("omega label"));
    }

    [Fact]
    public void SqlAssertedAgainstAWrappedCalledOrConditionalReceiver_IsNotData()
    {
        // Assert
        Assert.Contains(
            "pass_name = 'alpha_pass'",
            Commands()[0].CapturedCommandText,
            StringComparison.Ordinal);
        Assert.Contains("WHERE beta = true", Assert.Single(Commands()).ExecutedSql, StringComparison.Ordinal);
        Assert.True(Commands()[0].CapturedCommandText?.Contains("FROM gamma_cache", StringComparison.Ordinal) == true);
        var sql = Commands()[0].ExecutedSql;
        Assert.True(sql.IndexOf("FROM delta_entries d", StringComparison.Ordinal) >= 0);
        Assert.True(Commands()[0].ExecutedSql
            .Contains("INSERT INTO epsilon_concepts", StringComparison.Ordinal));
        Assert.Contains(Commands(), command => command.CommandText.Contains($"INSERT INTO {sql}", StringComparison.Ordinal));
    }

    [Fact]
    public void LiteralAgainstAWrappedOrIndexedNonSqlReceiver_IsFlagged()
    {
        // Assert
        Assert.Contains(
            "zeta label",
            Commands()[0].Name,
            StringComparison.Ordinal);
        var name = Commands()[0].Name;
        Assert.True(name.IndexOf("eta label", StringComparison.Ordinal) >= 0);
    }

    private static Predicate<CommandStub> Executed(string sqlFragment) =>
        command => command.ExecutedSql.Contains(sqlFragment, StringComparison.Ordinal);

    private static CommandStub Only(IReadOnlyList<CommandStub> commands, string sqlFragment) =>
        Assert.Single(commands, Executed(sqlFragment));

    private static Predicate<CommandStub> Labelled(string label) =>
        command => command.Name.Contains(label, StringComparison.Ordinal);

    private static IReadOnlyList<CommandStub> Commands() => [];

    private static string Chain(Func<string, string> map) => map(Guid.NewGuid().ToString());

    private static string Echo(string value) => value;

    private static IEnumerable<string> Seed() => [];

    private static string Deserialize(string payload) => payload;

    private static Guid ReadConceptTypeInATest(ProdFixture envelope) => Guid.Parse(envelope.OpaqueConceptType);

    private static Guid ReadSubInATest(string testOwnerSub) => Guid.Parse(testOwnerSub);

    private static bool VerifiesAnEmptyInput() => Verifier.Verify(string.Empty);

    private static Holder BlankInOneLineInitializer() => new Holder { Label = string.Empty };
}

public sealed class FixtureDbException : InvalidOperationException
{
    public FixtureDbException(bool isTransient) : base("fake provider failure") => IsTransient = isTransient;

    public bool IsTransient { get; }
}
'@

$prodTypeScript = @'
export const nonEmptyAssigned = 'Top level';
export const nonEmptyReturned = (nodeId: string): string => {
  return `node-${nodeId}`;
};
export const missing = (value: string | null): string => {
  if (value === null) {
    return '';
  }
  return value;
};

export const coalesced = (value: string | null): string => value ?? '';

export const labelled = (name: string, suffix = ''): string => name + suffix;

export const unchanged = (value: string): boolean => value === '';

export const joined = (parts: string[]): string => parts.join('');

export const letters = (word: string): string[] => word.split('');

export const trimmedSlash = (base: string): string => base.replace(/\/$/, '');

export const seeded = (parts: string[]): string => parts.reduce((all, part) => all + part, ``);

export const quotedLiteral = (): string => describe("a value ?? '' inside a string");

export const routeId = (params: Map<string, string>): string => params.get('id')!;

export const bodyReader = (response: Response) => response.body!.getReader();

export const lastEntry = (items: string[]): string => items.at(-1)!;

export const negated = (value: string | null): boolean => !value;

export const strictlyUnequal = (a: number, b: number): boolean => a !== b;

export const guarded = (value: string | null): string => (!value ? 'none' : value);

export const redisSocketOptions = { socketTimeout: 90_000, timeout: 5_000 };
'@

$e2eSpec = @'
import { LeafletClasses } from './leaflet-constants.js';
const CHURCH_ROWS = '[id^="church-row-"]';

test('selectors', async ({ page }) => {
  await expect(page.locator('.leaflet-marker-icon')).toBeVisible();
  await expect(page.locator('.library-table tbody tr td:first-child')).toBeVisible();
  await page.locator("input[name='Input.Email']").fill('x');
  await expect(page.locator('#church-title-0')).toBeVisible();
  await expect(page.locator('[id^="church-title-"]')).toBeAttached();
  await expect(page.locator(CHURCH_ROWS)).toHaveCount(2);
  await expect(page.locator(CHURCH_ROWS).first()).toBeVisible();
  await expect(page.locator(CHURCH_ROWS).nth(1)).toBeVisible();
});

test('getBy locators bind to copy', async ({ page }) => {
  await page.getByRole('button', { name: 'Save changes' }).click();
  await expect(page.getByText('No results found.')).toBeVisible();
  await page.getByLabel('Email address').fill('x');
  await page.getByPlaceholder('Search titles...').fill('x');
  await expect(page.getByRole('link', { name: 'Details' }).first()).toBeVisible();
  await expect(page.getByRole('main')).toBeVisible();
  await page.getByTestId('save-button').click();
});

test('assertions pin identity, never copy', async ({ page }) => {
  await expect(page.locator('#page-heading')).toHaveText('Clients');
  await expect(page.locator('#status-banner')).toContainText('Saved');
  await expect(page).toHaveTitle('Catalog');
  expect(await page.locator('#nav-label-0').textContent()).toBe('Library');
  await expect(page.locator('#name-input')).toHaveValue(expectedName);
  await expect(page.locator('#details-link')).toHaveAttribute('href', expectedHref);
  await expect(page).toHaveURL(expectedUrl);
  await expect(page.locator('#result-rows')).toHaveCount(expectedRowCount);
  await expect(page.locator('#load-error')).toContainText(LOAD_ERROR_MESSAGE);
  await expect(page.locator('#page-heading')).toHaveText(PageTitles.clients);
  await expect(page.locator('#run-id')).toContainText(seededRunId);
});

test('a value generated in this file is not copy', async ({ page }) => {
  const savedRawgKey = randomUUID();
  const generatedTitle = crypto.randomUUID();
  await expect(page.locator('#key-panel')).not.toContainText(savedRawgKey);
  await expect(page.locator('#title-cell')).toContainText(generatedTitle);
});

test('a value a step minted into the shared context is not copy', async ({ page }) => {
  ctx.mintedAccountId = randomUUID();
  ctx.copiedBanner = 'Saved';
  await expect(page.locator('#page-title')).not.toContainText(ctx.mintedAccountId);
  await expect(page.locator('#status-banner')).toContainText(ctx.copiedBanner);
});

test('the whole page is never read as text', async ({ page }) => {
  const visibleText = await page.locator('body').innerText();
  const markup = await page.content();
  expect(await page.textContent('body')).toContain(expectedName);
  await expect(page.locator('#page-body')).toBeVisible();
  const heading = await page.locator('#page-heading').getAttribute('data-name');
});

const RENDER_TIMEOUT_MS = 30_000;

test('waits carry no timeout of their own', async ({ page }) => {
  page.setDefaultTimeout(60_000);
  test.setTimeout(120_000);
  test.slow();
  await expect(page.locator('#status-banner')).toBeVisible({ timeout: 10_000 });
  await page.waitForURL('**/done', { timeout: RENDER_TIMEOUT_MS });
  await page.waitForTimeout(500);
  await new Promise<void>(resolve => setTimeout(resolve, 250));
  await expect(page.locator('#result-panel')).toContainText('timeout: none');
});

async function waitForRender(page: Page, renderTimeoutMs: number): Promise<void> {
  await expect(page.locator('#render-root')).toBeVisible();
}

export function scheduleRunCompletion(finish: () => void): void {
  setTimeout(() => {
    finish();
  }, 300);
}
'@

# The .NET Playwright dialect. The CSS-string patterns that catch the TypeScript spec above are blind to
# every line here, which is how a tag+text+id-prefix+.First scan sat unflagged in Identity's E2E suite.
$e2eCSharp = @'
namespace FixtureApp.E2E;

public class SelectorFixtureTests
{
    public async Task NavigateAsync(IPage page, string email)
    {
        var row = page.Locator("tr", new PageLocatorOptions { HasText = email });
        var wholeSet = page.Locator("[id^='details-']");
        var everything = page.Locator("table");
        var disambiguated = page.Locator("#details-0").First;
        var prefixThenPosition = page.Locator("[id^='details-']").First;
        var linqIsNotPosition = page.Locator("#details-0").GetAttribute("id").First();
        var namesOnAnArgument = Assertions.Expect(page.Locator("#details-table")).Not.ToHaveAttributeAsync("data-name", names.First);
        await Assertions.Expect(page.Locator("#page-heading")).ToHaveTextAsync("Clients");
        var generatedClientName = Guid.NewGuid().ToString();
        await Assertions.Expect(page.Locator("#client-name")).ToHaveTextAsync(generatedClientName);
        Assert.Equal("Clients", await page.Locator("#page-heading").TextContentAsync());
        var bodyText = await page.TextContentAsync("body");
        var pageMarkup = await page.ContentAsync();
        var bodyInnerText = await page.Locator("body").InnerTextAsync();
        await page.Locator("input[name='Input.Email']").FillAsync("x");
        await page.Locator("#details-0").ClickAsync();
    }

    private const float CeremonyTimeoutMs = 30_000;

    public async Task WaitsCarryNoTimeoutAsync(IPage page)
    {
        page.SetDefaultTimeout(60_000);
        await Assertions.Expect(page).ToHaveURLAsync(new Regex("/home"), new PageAssertionsToHaveURLOptions { Timeout = 60_000 });
        await page.Locator("#sign-in").ClickAsync(new LocatorClickOptions { Timeout = CeremonyTimeoutMs });
        await page.WaitForURLAsync(
            "**/done",
            new PageWaitForURLOptions
            {
                Timeout = 10_000
            });
        await page.WaitForTimeoutAsync(500);
        await Task.Delay(100);
        var timeoutLabel = "Timeout = 5";
        Assert.NotNull(timeoutLabel);
    }

    public async Task<string> WaitForMailAsync(string toAddress, TimeSpan? timeout = null)
    {
        try
        {
            return await ReadMailAsync(toAddress);
        }
        catch (Exception exception) when (exception is TimeoutException or PlaywrightException)
        {
            return toAddress;
        }
    }
}
'@

$authHandlerCode = @'
namespace FixtureApp;

public sealed class FixtureAuthHandler : AuthenticationHandler<AuthenticationSchemeOptions>
{
    public FixtureAuthHandler(
        IOptionsMonitor<AuthenticationSchemeOptions> options,
        ILoggerFactory logger,
        UrlEncoder encoder)
        : base(options, logger, encoder)
    {
    }

    public void RemoveHostLogging(IServiceCollection services)
    {
        services.RemoveAll<ILoggerFactory>();
    }
}
'@

# Rule 13's exemption is the pass-through SHAPE, not a base-type allow-list, so a second base class with the
# same constructor demand must be exempt too - and this one takes ILogger<T> rather than ILoggerFactory.
$derivedProcessorCode = @'
namespace FixtureApp;

public sealed class FixtureDerivedProcessor : ProcessorBase
{
    public FixtureDerivedProcessor(ILogger<FixtureDerivedProcessor> logger, string queueName)
        : base(logger, queueName)
    {
    }
}
'@

# A primary constructor names the base TYPE where an ordinary one writes `base`, and the fleet uses both
# (Identity.Tests.E2E/Infrastructure/FakeExternalAuthenticationHandler.cs is the live instance). Matching
# only `: base(` left every primary-constructor handler reporting.
$primaryCtorHandlerCode = @'
namespace FixtureApp;

public sealed class FixturePrimaryCtorHandler(
    IOptionsMonitor<AuthenticationSchemeOptions> options,
    ILoggerFactory logger,
    UrlEncoder encoder)
    : AuthenticationHandler<AuthenticationSchemeOptions>(options, logger, encoder)
{
}
'@

# The opposite-sign control, and the case the old file-level flag silently exempted: the same base
# constructor, the same parameter, but this class KEEPS it. It now holds a logger of its own, which is
# exactly what rule 13 bans, so it must still report. Its own file so the file-wide assignment scan
# cannot reach across into FixtureAuthHandler.
$keptLoggerHandlerCode = @'
namespace FixtureApp;

public sealed class FixtureKeptLoggerHandler : AuthenticationHandler<AuthenticationSchemeOptions>
{
    private readonly ILoggerFactory _loggerFactory;

    public FixtureKeptLoggerHandler(
        IOptionsMonitor<AuthenticationSchemeOptions> options,
        ILoggerFactory logger,
        UrlEncoder encoder)
        : base(options, logger, encoder)
    {
        _loggerFactory = logger;
    }
}
'@

# Rule 2's inverse arm exists for the OData EDM case, so it is judged per REPO: a csproj referencing OData
# arms it, and a repo without one (PlainApp) keeps its Uri properties out of the queue.
$odataProject = @'
<Project Sdk="Microsoft.NET.Sdk.Web">
  <ItemGroup>
    <PackageReference Include="Microsoft.AspNetCore.OData" />
  </ItemGroup>
</Project>
'@

# Where rule 2's OData arm stops: a request type the model builder Ignore<>s is not in the EDM at all, so
# its Uri property cannot become a complex type and cannot 400 a PATCH. SchemaDeclaredUri on ProdFixture is
# the opposite-sign control - same property shape, same repo, not ignored, still reported.
$edmIgnoredCode = @'
namespace FixtureApp;

public class EdmIgnoredRequestFixture
{
    public Uri? ManualUrl { get; set; }

    public void KeepOwner(string ownerSub) => _ = ownerSub;
}
'@

# A page model whose *Url string property sits under an .editorconfig scope that silences CA1056 with a
# recorded reason (Identity's model-bound relative return URLs). The catalog mirrors that analyzer, so it
# honours the same scope; the ManualUrl property in ProdFixture.cs sits outside the scope and still fires.
$editorConfig = @'
root = true

[FixtureApp/Pages/**.cs]
dotnet_diagnostic.CA1054.severity = none
dotnet_diagnostic.CA1056.severity = none
'@

$pageModelCode = @'
namespace FixtureApp.Pages;

public class LoginModel
{
    public string? ReturnUrl { get; set; }
}
'@

$plainOptionsCode = @'
namespace PlainApp;

public class PlainOptions
{
    public Uri? VerifyEndpoint { get; set; }
    public string? ParsedRunId { get; init; }
}
'@

# A per-project generator class, the shape rule 11 retires: `Shared.Testing.Generated` is the only generator
# class, so every `New*` method here must report `move-to-generated`, and the filename earns no exemption
# for the literals beside them.
$testValuesCode = @'
namespace FixtureApp.Tests.Unit.TestSupport;

internal static class TestValues
{
    private const string Punctuation = "!@#$%^&*()";

    private const string LegacySuffix = "-legacy";

    internal static string NewCity() => LowercaseToken(9);

    internal static string NewPunctuationToken() =>
        string.Concat(Enumerable.Range(0, 3).Select(_ => Punctuation[Random.Shared.Next(Punctuation.Length)]));

    internal static string NewUserId() => $"user-{LowercaseToken(12)}";

    internal static string NewZip() => Random.Shared.Next(10000, 100000).ToString(CultureInfo.InvariantCulture);

    internal static string NewBlankRun()
    {
        return new string(' ', Random.Shared.Next(1, 4));
    }

    internal static string HardcodedDenomination() => "baptist";

    internal static string LegacyDenomination() => HardcodedDenomination() + LegacySuffix;

    private static string LowercaseToken(int length) =>
        string.Concat(Enumerable.Range(0, length).Select(_ => (char)Random.Shared.Next('a', 'z' + 1)));
}
'@

# A dedicated constants class is the fleet's place for a named constant, and it IS the end state for a
# literal: the value is named once, in one place, and the name says what it is. Rule 11's complaint is an
# UNNAMED literal at a use site, so flagging the declaration is flagging the fix.
#
# The file must EARN the name: if the filename alone were enough,
# anyone could silence a row by renaming a file. So the whole type must be a static class whose every
# member is const or static readonly. The second fixture is a file that merely ends in Constants.cs and
# carries ordinary test code beside a literal - it must keep reporting.
$constantsCode = @'
namespace FixtureApp.Tests.Unit;

internal static class FixtureEventConstants
{
    internal const int DroppedEventId = 263521618;
    internal const string LicenseExpiredEventName = "LicenseExpired";
    internal const string AuthorizationHeaderName = "Authorization";
    internal static readonly TimeSpan PingInterval = TimeSpan.FromMilliseconds(50);
}
'@

# An attribute argument must be a constant expression, so a const an attribute in the SAME file names is
# compelled by the language rather than chosen. Deliberately NOT named *Constants.cs: that would hit the
# constants-class exemption instead and this fixture would pass without exercising the attribute rule at
# all. The second const is the control - same file, same shape, named by no attribute, still reports.
$attributeHostCode = @'
namespace FixtureApp.Tests.Unit;

[CollectionDefinition(AttributeCompelledName)]
public sealed class FixtureAttributeHost
{
    public const string AttributeCompelledName = "Unit";

    public const string NoAttributeNamesThisOne = "denomination";
}
'@

$fakeConstantsCode = @'
namespace FixtureApp.Tests.Unit;

internal static class NotReallyConstants
{
    internal const string DeclaredName = "LicenseExpired";

    internal static string Describe(string denomination)
    {
        var label = "baptist";
        return label + denomination;
    }
}
'@

# Rule 11 sweeps every test area, not only the unit one. This fixture sits in the e2e directory, which
# Get-Area reads BEFORE a project name, so it is where the old unit-only gate left whole suites unscanned.
# The generated value beside the magic one is the control: widening the area must not make the rule fire on
# a value that is already generated.
$e2eDataCode = @'
namespace FixtureApp.Tests.E2E;

public sealed class E2EDataFixtureTests
{
    private const int DefaultSteps = 40;
    private const int MinSteps = 2;
    private const int MaxSteps = 500;
    private const int DefaultShare = 25;
    private const int MinShare = 1;
    private const int MaxShare = 99;
    private const int UnrelatedBudget = 77;
    private const string DefaultLabel = "walker-default";
    private const string HeadedVariable = "FIXTURE_HEADED";
    private const string HeadedEnabledValue = "headed-on";
    private const string PageTitleScript = "() => document.title";
    private const string WrappedHrefScript =
        "() => window.location.href";

    private static async Task RunsBrowserCode(IPage page)
    {
        await page.EvaluateAsync<string>("() => document.body.id");
        await page.AddInitScriptAsync(
            "window.fixtureFlag = true;");
        await page.EvaluateAsync<string>(PageTitleScript);
        await page.WaitForFunctionAsync(WrappedHrefScript);
        await page.EvaluateAsync<string>("() => document.dir", "fixture-script-argument");
        await page.EvaluateHandleAsync("""
            () => document.querySelector('main')
            """);
        await page.EvaluateAsync<int>(
            @"() => document.forms.length
                + document.images.length");
    }

    [SuppressMessage(
        "FixtureDesign",
        "CA1031:Fixture general catch",
        Justification = "Fixture diagnostics never fail the test.")]
    private static string AfterTheAttribute(string mode = "fixture-default-mode") => mode;

    [Fact]
    public async Task SeedsAChurch()
    {
        // Arrange
        var generatedCity = Generated.NewCity();
        var seededCity = "Phoenix";

        // Act
        var created = await Seed(seededCity, generatedCity);

        // Assert
        Assert.NotNull(created);
    }

    [Fact]
    public async Task DrivesTheBrowser()
    {
        // Arrange
        await page.GotoAsync("/admin/users");
        await page.GotoAsync($"{AuthorizationNames.AdminFolder}/Clients/Edit?id={clientId}");
        await page.GotoAsync($"{sectionFolder}/Roles/Edit");
        await page.RouteAsync("**/products/api/**", route => route.ContinueAsync());
        await page.ClickAsync("#save-button");
        await page.FillAsync("#price-paid", "999");
        var routeShaped = Describe("/not-a-route");

        // Act
        await Assertions.Expect(page).ToHaveURLAsync(new Regex("/admin/users/details"));
        var response = await _client.PostAsJsonAsync("/churches", body);
        var wrapped = await _client.PostAsJsonAsync(
            "/chats/messages",
            new ChatRequest("Where is the manual?"));
        using var built = new HttpRequestMessage(HttpMethod.Post, "/chats/stream")
        {
            Content = new StringContent("streamed body"),
        };
        _fixture.CatalogStore.Create("LG OLED TV");

        // Assert
        Assert.Equal(routeShaped, body.GetProperty("slug").GetString());
        var lookedUp = Lookup("slug");
    }

    private static void AssertsRoutes(IPage page, string bodyText)
    {
        Assert.DoesNotContain("/Account/Login", page.Url, StringComparison.Ordinal);
        Assert.DoesNotContain("/Account/Signup", bodyText, StringComparison.Ordinal);
        var onCallback = page.Url.Contains("localhost:9999", StringComparison.Ordinal);
        await page.WaitForURLAsync(url => url.Contains("/Account/Lockout", StringComparison.Ordinal));
        Assert.Equal("/Admin/Clients", new Uri(page.Url).AbsolutePath);
        Assert.Equal("/Admin/Roles", bodyText);
        Assert.Equal("https://cdn.example/avatar.jpg", user.AvatarUrl);
        Assert.Equal("https://example.test/manual.pdf", product.ManualUrl);
    }

    private static void ReadsACacheKey(IDistributedCache cache)
    {
        var cached = cache.GetAsync("catalog-page-two");
        var borrowedKey = cache.GetAsync($"{CacheKeys.CatalogPrefix}:page-three");
    }

    private static void ReadsAJsonPayload(JsonElement root)
    {
        var readName = ReadString(root, "name");
        var describedName = Describe(root, "name-not-a-property");
    }

    private static string? ReadString(JsonElement root, string propertyName) =>
        root.TryGetProperty(propertyName, out var value) ? value.GetString() : null;

    private static string Describe(JsonElement root, string label) => label;

    private static void ReadsODataOptions(IQueryCollection query, Dictionary<string, object?> response)
    {
        query.TryGetValue("$filter", out var filter);
        query.TryGetValue("$orderby", out var orderBy);
        response["@odata.count"] = 0;
        query.TryGetValue("$notAnODataOption", out var invented);
        query.TryGetValue("pageSize", out var pageSize);
    }

    private static string HoldsAnAtSign()
    {
        var atSign = "@";
        var afterTheAtSign = "Scottsdale";
        return atSign + afterTheAtSign;
    }

    private static string StripsFormatting(string sharedKey, string city)
    {
        var stripped = sharedKey.Replace(" ", string.Empty, StringComparison.Ordinal);
        return stripped.Replace("Phoenix", city, StringComparison.Ordinal);
    }

    private static void SendsHeaders(HttpRequestMessage request)
    {
        Assert.SkipUnless(request is not null, "Set NPSSO to run this probe.");
        request.Headers.Remove("x-psn-store-locale-override");
        request.Headers.Add("apollo-require-preflight", "true");
        request.Headers.TryAddWithoutValidation("User-Agent", "crgolden-probe/1.0");
    }

    private const string DefaultLinkPrefix = "http";

    private static string ExtractLink(string body, string urlPrefix = DefaultLinkPrefix) => urlPrefix + body;

    private static void NamesActions()
    {
        var railId = "#nav-home";
        var cards = "[id^='admin-card-']";
        var byClass = ".catalog-title";
        var rail = NavigateAction("home", CommonWeight, railId);
    }

    private static void BindsParameters(SqlCommand cmd)
    {
        cmd.Parameters.AddWithValue("@Id", id);
        cmd.Parameters.AddWithValue("@City", "Tucson");
        var runId = Environment.GetEnvironmentVariable("CI_RUN_ID");
        Environment.SetEnvironmentVariable("CI_RUN_ID", "build-77");
        await Assertions.Expect(page.Locator("#login-errors")).ToHaveAttributeAsync("data-error-code", expectedCode);
        await Assertions.Expect(page.Locator("#login-status")).ToHaveAttributeAsync("data-state", "suspended");
        var steps = EnvironmentSetting.Count("SYNTHETIC_STEPS", DefaultSteps, MinSteps, MaxSteps);
        var share = EnvironmentSetting.Percent("SYNTHETIC_SHARE", DefaultShare, MinShare, MaxShare);
        var label = EnvironmentSetting.Text("SYNTHETIC_LABEL", DefaultLabel);
        var tag = EnvironmentSetting.Text("SYNTHETIC_TAG", "walker-7");
        var headed = string.Equals(Environment.GetEnvironmentVariable(HeadedVariable), HeadedEnabledValue, StringComparison.Ordinal);
    }

    private static string Verb(string method)
    {
        var fallbackVerb = "PATCH";
        switch (method)
        {
            case "GET":
                return fallbackVerb;
            default:
                return method;
        }
    }
}
'@

# A multi-line `@"..."` carrying JavaScript. Every literal and number in the body belongs to the embedded
# language, so the block earns ONE needs-triage row at its opening and nothing from its insides. The
# single-line verbatim below is the opposite control: it closes on its own line, so the ordinary sweep
# must still reach the code beside it.
$verbatimBlockCode = @'
namespace FixtureApp.Tests.E2E;

public sealed class VerbatimBlockFixtureTests
{
    public string SeedScript()
    {
        var script = @"
            const button = document.querySelector('#passkey-submit');
            const attempts = 7;
            button.setAttribute('data-timeout', '60000');
        ";
        return script;
    }

    public string ClosedOnOneLine() => Describe(@"still-swept-literal");

    private static string Describe(string value) => value;
}
'@

$pagesDir = Join-Path $prodDir 'Pages'
$plainDir = Join-Path $fixtureRoot 'PlainApp\PlainApp'
$testSupportDir = Join-Path $unitDir 'TestSupport'
foreach ($d in @($pagesDir, $plainDir, $testSupportDir)) { New-Item -ItemType Directory -Path $d -Force | Out-Null }
Set-Content -LiteralPath (Join-Path $testSupportDir 'TestValues.cs') -Value $testValuesCode -Encoding UTF8
Set-Content -LiteralPath (Join-Path $unitDir 'FixtureEventConstants.cs') -Value $constantsCode -Encoding UTF8
Set-Content -LiteralPath (Join-Path $unitDir 'NotReallyConstants.cs') -Value $fakeConstantsCode -Encoding UTF8
Set-Content -LiteralPath (Join-Path $unitDir 'FixtureAttributeHost.cs') -Value $attributeHostCode -Encoding UTF8
Set-Content -LiteralPath (Join-Path $prodDir 'FixtureApp.csproj') -Value $odataProject -Encoding UTF8
Set-Content -LiteralPath (Join-Path $fixtureRoot 'FixtureApp\.editorconfig') -Value $editorConfig -Encoding UTF8
Set-Content -LiteralPath (Join-Path $pagesDir 'Login.cshtml.cs') -Value $pageModelCode -Encoding UTF8
Set-Content -LiteralPath (Join-Path $plainDir 'PlainOptions.cs') -Value $plainOptionsCode -Encoding UTF8
Set-Content -LiteralPath (Join-Path $e2eDir 'SelectorFixtureTests.cs') -Value $e2eCSharp -Encoding UTF8
Set-Content -LiteralPath (Join-Path $e2eDir 'E2EDataFixtureTests.cs') -Value $e2eDataCode -Encoding UTF8
Set-Content -LiteralPath (Join-Path $e2eDir 'VerbatimBlockFixtureTests.cs') -Value $verbatimBlockCode -Encoding UTF8
Set-Content -LiteralPath (Join-Path $prodDir 'ProdFixture.cs') -Value $prodCode -Encoding UTF8
Set-Content -LiteralPath (Join-Path $prodDir 'EdmIgnoredRequestFixture.cs') -Value $edmIgnoredCode -Encoding UTF8
# A load tier drives endpoints over HTTP like an integration tier, so its project directory is what makes it
# test code. The file name ends in neither Tests.cs nor Test.cs, so only the directory can classify it.
$loadDir = Join-Path $fixtureRoot 'FixtureApp\FixtureApp.Tests.Load'
New-Item -ItemType Directory -Path $loadDir -Force | Out-Null
Set-Content -LiteralPath (Join-Path $loadDir 'LoadAreaFixture.cs') -Value @'
namespace FixtureApp.Tests.Load;

public sealed class LoadAreaFixture
{
    public void Budget(CancellationTokenSource loadDeadline) => loadDeadline.CancelAfter(TimeSpan.FromSeconds(5));
}
'@ -Encoding UTF8
# A separate file on purpose: the exemption is decided by the type's BASE CLASS, so it has to be judged
# per file rather than per line. Keeping it beside a non-handler fixture is what proves the flag is scoped.
Set-Content -LiteralPath (Join-Path $prodDir 'FixtureAuthHandler.cs') -Value $authHandlerCode -Encoding UTF8
Set-Content -LiteralPath (Join-Path $prodDir 'FixtureDerivedProcessor.cs') -Value $derivedProcessorCode -Encoding UTF8
Set-Content -LiteralPath (Join-Path $prodDir 'FixturePrimaryCtorHandler.cs') -Value $primaryCtorHandlerCode -Encoding UTF8
Set-Content -LiteralPath (Join-Path $prodDir 'FixtureKeptLoggerHandler.cs') -Value $keptLoggerHandlerCode -Encoding UTF8
Set-Content -LiteralPath (Join-Path $prodDir 'prod.ts') -Value $prodTypeScript -Encoding UTF8
Set-Content -LiteralPath (Join-Path $unitDir 'FixtureTests.cs') -Value $unitCode -Encoding UTF8
$theoryDataCode = @'
namespace FixtureApp.Tests.Unit;

public class FixtureTheoryDataTests
{
    private static readonly string GeneratedFieldValue = Generated.NewUserName();

    public static TheoryData<string?> MixedRowsSource() => new()
    {
        (string?)null,
        Generated.NewWhitespaceValue(),
    };

    public static TheoryData<string> BlockBodiedGeneratedSource()
    {
        var drawnValue = Guid.NewGuid().ToString();
        return new TheoryData<string> { drawnValue };
    }

    public static TheoryData<string> GeneratedFieldRowsSource => new()
    {
        GeneratedFieldValue,
    };

    public static TheoryData<string?> StaticRowsSource() => new()
    {
        (string?)null,
        string.Empty,
    };

    public static TheoryData<string> UnnamedGeneratedHelper() => new()
    {
        Generated.NewUserName(),
    };

    [Theory]
    [MemberData(nameof(MixedRowsSource))]
    public void MixedRows_Accepted(string? value)
    {
        // Act
        var accepted = Sut.Accept(value);

        // Assert
        Assert.True(accepted);
    }

    [Theory]
    [MemberData(nameof(BlockBodiedGeneratedSource))]
    public void BlockBodied_Accepted(string value)
    {
        // Act
        var accepted = Sut.Accept(value);

        // Assert
        Assert.True(accepted);
    }

    [Theory]
    [MemberData(nameof(GeneratedFieldRowsSource))]
    public void GeneratedField_Accepted(string value)
    {
        // Act
        var accepted = Sut.Accept(value);

        // Assert
        Assert.True(accepted);
    }

    [Theory]
    [MemberData(nameof(StaticRowsSource))]
    public void StaticRows_Rejected(string? value)
    {
        // Act
        var accepted = Sut.Accept(value);

        // Assert
        Assert.False(accepted);
    }
}
'@
Set-Content -LiteralPath (Join-Path $unitDir 'FixtureTheoryDataTests.cs') -Value $theoryDataCode -Encoding UTF8
Set-Content -LiteralPath (Join-Path $e2eDir 'fixture.spec.ts') -Value $e2eSpec -Encoding UTF8

# Rule 17 is judged by Area, and three of its areas are decided by a file name or a directory rather than an
# `e2e\`/`.Tests.*\` path: a runner config, a Vitest spec beside its source, and the walker engine in a src
# tree. Each gets a file whose only route into the rule is that classification.
$playwrightConfig = @'
export default defineConfig({
  timeout: 10 * 60_000,
  expect: { timeout: 15_000 },
  use: {
    actionTimeout: 30_000,
    navigationTimeout: 60_000,
  },
  webServer: {
    command: 'npm start',
    timeout: 60_000,
  },
});
'@

$vitestConfig = @'
export default defineConfig({
  test: {
    pool: 'threads',
    testTimeout: 15000,
  },
});
'@

$colocatedSpec = @'
describe('session store', () => {
  vi.setConfig({ testTimeout: 20_000 });

  it('reconnects after a dropped socket', async () => {
    await store.reconnect();
  });
});
'@

$moduleVitestConfig = @'
export default defineConfig({
  test: {
    testTimeout: 25_000,
  },
});
'@

$moduleColocatedSpec = @'
describe('module session store', () => {
  vi.setConfig({ hookTimeout: 30_000 });
});
'@

$walkerEngine = @'
export async function submitPasskey(page: Page): Promise<void> {
  await page.locator('#passkey-submit').click({ timeout: PASSKEY_SUBMIT_TIMEOUT_MS });
}
'@

$walkerDir = Join-Path $fixtureRoot 'FixtureApp\src\synthetic-walker'
New-Item -ItemType Directory -Path $walkerDir -Force | Out-Null
Set-Content -LiteralPath (Join-Path $fixtureRoot 'FixtureApp\playwright.config.ts') -Value $playwrightConfig -Encoding UTF8
Set-Content -LiteralPath (Join-Path $fixtureRoot 'FixtureApp\vitest.config.ts') -Value $vitestConfig -Encoding UTF8
Set-Content -LiteralPath (Join-Path $prodDir 'session.spec.ts') -Value $colocatedSpec -Encoding UTF8
Set-Content -LiteralPath (Join-Path $fixtureRoot 'FixtureApp\vitest.config.mts') -Value $moduleVitestConfig -Encoding UTF8
Set-Content -LiteralPath (Join-Path $prodDir 'session.spec.mts') -Value $moduleColocatedSpec -Encoding UTF8
Set-Content -LiteralPath (Join-Path $walkerDir 'walker.ts') -Value $walkerEngine -Encoding UTF8

# The operator enrollment script shares the walker directory and waits minutes for a human to sign in.
$operatorEnrollment = @'
export async function enrollPasskey(page: Page): Promise<void> {
  await page.waitForURL(url => url.pathname.startsWith(MANAGE_PASSKEYS_PATH), { timeout: ENROLLMENT_TIMEOUT_MS });
}
'@
Set-Content -LiteralPath (Join-Path $walkerDir 'enroll-passkey.ts') -Value $operatorEnrollment -Encoding UTF8

# Modules compiles its tests into a gitignored dist-test tree that mirrors src, walker directory included, so
# once Get-Area read `synthetic-walker\` as E2E code the compiled declaration copy reported beside its source.
$compiledWalkerDir = Join-Path $fixtureRoot 'FixtureApp\dist-test\synthetic-walker'
New-Item -ItemType Directory -Path $compiledWalkerDir -Force | Out-Null
$compiledWalker = @'
export declare const HYDRATION_TIMEOUT_MS = 30000;
export declare function waitForAngularHydration(page: Page, timeoutMs?: number): Promise<void>;
'@
Set-Content -LiteralPath (Join-Path $compiledWalkerDir 'compiled-walker.d.ts') -Value $compiledWalker -Encoding UTF8

$siteTestsDir = Join-Path $fixtureRoot 'FixtureSite\tests'
New-Item -ItemType Directory -Path $siteTestsDir -Force | Out-Null
$siteSpec = @'
test('the about page renders', async ({ page }) => {
  await expect(page.locator('#about-heading')).toHaveText('About the site');
});
'@
Set-Content -LiteralPath (Join-Path $siteTestsDir 'site.spec.ts') -Value $siteSpec -Encoding UTF8
$colocatedCopySpec = @'
it('renders the heading', () => {
  expect(compiled.querySelector('#widget-heading')?.textContent).toContain('Widget heading');
});
'@
Set-Content -LiteralPath (Join-Path $prodDir 'widget.spec.ts') -Value $colocatedCopySpec -Encoding UTF8

# Rule 11's copied-production-text check compares a repo's test literals with its production literals in
# three languages. Each language gets a production file and a test file. The controls: a literal production
# never says, a word under the length floor, the pin shape rule 11 mandates, a module specifier, and the
# same text in a DIFFERENT repo's test.
$copySourceCSharp = @'
namespace FixtureApp;

public static class CopySource
{
    public const string ReadyMessage = "Widget pipeline ready";

    public const string ShortWord = "ready";

    public const string Grouping = "Category";

    public static InvalidOperationException Missing(string key) => new($"Invalid '{key}'.");
}
'@

$copyTestsCSharp = @'
namespace FixtureApp.Tests.Unit;

[Trait("Category", "Unit")]
public sealed class CopyFixtureTests
{
    [Fact]
    public void Copies()
    {
        var restatedMessage = "Widget pipeline ready";
        var renderedTemplate = "Invalid 'OrderQueue'.";
        var restatedTemplate = $"Invalid '{Seed()}'.";
        var neverSaidByProduction = "Gadget assembly idle";
        var underTheFloor = "ready";
        Assert.Equal("Widget pipeline ready", CopySource.ReadyMessage);
        Assert.Equal("Widget pipeline ready", ready.Text);
    }

    [Fact]
    public void PinsReadUnderAct()
    {
        var readyMessage = CopySource.ReadyMessage;
        Assert.Equal("Widget pipeline ready", readyMessage);
        Assert.Equal(["Widget pipeline ready", "Invalid 'OrderQueue'."], CopySource.AllMessages);
    }
}
'@

$plainCopyTests = @'
namespace PlainApp.Tests.Unit;

public sealed class PlainCopyTests
{
    [Fact]
    public void AnotherRepoSaysTheSameWords()
    {
        var anotherReposText = "Widget pipeline ready";
    }
}
'@

$copySourceTypeScript = @'
import { thing } from './settings-module';

export const READY_LOG = '[Worker] pipeline ready';

export function missing(key: string): Error {
  return new Error(`Invalid '${key}'.`);
}

export function isText(value: unknown): boolean {
  return typeof value === 'string';
}
'@

$copySpecTypeScript = @'
import { thing } from './settings-module';

vi.mock('./settings-module');

it('copies', () => {
  expect(log).toHaveBeenCalledWith('[Worker] pipeline ready');
  expect(() => run()).toThrow("Invalid 'OrderQueue'.");
  expect(READY_LOG).toBe('[Worker] pipeline ready');
  expect(ready).toBe('[Worker] pipeline ready');
  expect(message).toBe('Gadget assembly idle');
  expect(typeof value === 'string').toBe(true);
  expect(kind).toBe('string');
  expect(RETRY_LIMIT).toBe(42);
  expect(retries).toBe(43);
  const joined = parts.join('\n');
  const heading = '\nWidget heading';
  const origin = `${scheme}://${host}`;
  const encoded = `${before}%26${after}`;
  const later = 'Galveston harbor';
  const cover = `${prefix}${encodeURIComponent(
    `<svg xmlns="${ns}" width="${side}"></svg>`,
  )}`;
  const afterNested = 'Galveston ferry';
});

describe('[Worker] pipeline ready', () => {
  test('[Worker] pipeline ready', () => {});
});
'@

$copySourcePython = @'
READY_LOG = "Worker pipeline ready"
TERMINAL_STATUSES = ("succeeded", "cancelled")
_DATA_STREAM = "logs-app-widget"
COUNT_SQL = "SELECT COUNT(*) FROM widgets WHERE gen.name = %s"
CLEANUP_SQL = "DELETE FROM widget_owners WHERE owner_sub = %s"
PLATFORM_FIELD = "platform"
RECORD_KEY = "params"
WIRE_KEY = "categoryId"
DOC_LINE = "Widgets are counted once per owner."
SCHEMA_FILTER = "WHERE widget_schema = 'public' "
SUMMARY_LINE = "Owners are summed per widget."


def missing(key: str) -> ValueError:
    return ValueError(f"Invalid {key!r} setting.")


if __name__ == "__main__":
    raise missing(READY_LOG)
'@

$copyTestsPython = @'
from pyapp.copy_source import READY_LOG


def test_copies():
    assert log == "Worker pipeline ready"
    assert str(error) == "Invalid 'OrderQueue' setting."
    assert READY_LOG == "Worker pipeline ready"
    assert message == "Gadget assembly idle"
    assert "SELECT COUNT(*) FROM widgets WHERE gen.name = %s" in count_sql
    cur.execute("DELETE FROM widget_owners WHERE owner_sub = %s", (sub,))
    assert TERMINAL_STATUSES == ("succeeded", "cancelled")
    assert copy_source._DATA_STREAM == "logs-app-widget"
    recorded = call["params"]
    client.get(url, params={"categoryId": category})
    lookup = fetch("Worker pipeline ready")


def test_wrapped_statements():
    cur.execute(
        "SELECT COUNT(*) FROM widgets WHERE gen.name = %s",
        (name,),
    )
    assert (
        copy_source.TERMINAL_STATUSES,
    ) == ("succeeded", "cancelled")
    assert copy_source.LEVEL_NAMES == {
        "DEBUG": "Worker pipeline ready",
    }
    summary = fetch(
        "Widgets are counted once per owner.",
    )
    cur.execute(
        "SELECT widget_name FROM widget_columns "
        "WHERE widget_schema = 'public' "
        "AND widget_name = %s",
        (name,),
    )
    totals = summarize(
        "Totals: "
        "Owners are summed per widget.",
    )


@pytest.mark.parametrize(
    "platform",
    PLATFORMS,
)
def test_wrapped_platforms(platform):
    pass


@pytest.mark.parametrize("platform", PLATFORMS)
def test_platforms(platform):
    """Widgets are counted once per owner."""


if __name__ == "__main__":
    test_copies()
'@

$plainTestsDir = Join-Path $fixtureRoot 'PlainApp\PlainApp.Tests.Unit'
$pySourceDir = Join-Path $fixtureRoot 'PyApp\src\pyapp'
$pyTestsDir = Join-Path $fixtureRoot 'PyApp\tests'
foreach ($d in @($plainTestsDir, $pySourceDir, $pyTestsDir)) { New-Item -ItemType Directory -Path $d -Force | Out-Null }
Set-Content -LiteralPath (Join-Path $prodDir 'CopySource.cs') -Value $copySourceCSharp -Encoding UTF8
Set-Content -LiteralPath (Join-Path $unitDir 'CopyFixtureTests.cs') -Value $copyTestsCSharp -Encoding UTF8
Set-Content -LiteralPath (Join-Path $plainTestsDir 'PlainCopyTests.cs') -Value $plainCopyTests -Encoding UTF8
Set-Content -LiteralPath (Join-Path $prodDir 'copy-source.ts') -Value $copySourceTypeScript -Encoding UTF8
Set-Content -LiteralPath (Join-Path $prodDir 'copy-source.spec.ts') -Value $copySpecTypeScript -Encoding UTF8
$vendorConstantsTypeScript = @'
export const WorkerLibraryConstants = {
  readyEvent: '[Worker] pipeline ready',
  levels: ['[Worker] pipeline ready'],
} as const;
'@

$lookalikeConstantsTypeScript = @'
export const LookalikeConstants = {
  readyEvent: '[Worker] pipeline ready',
} as const;

export function readyEventFor(page: string): string {
  return `${page} ${LookalikeConstants.readyEvent}`;
}
'@

$vendorConstantsCSharp = @'
namespace FixtureApp.Tests.Unit;

internal static class WidgetLibraryConstants
{
    internal const string ReadyEvent = "Widget pipeline ready";
}
'@

$magicLiteralSpec = @'
import { expect, test } from '@playwright/test';
import {
  seededChurch,
} from './church-fixtures';

test('seeds a church and reads it back', async ({ page }) => {
  const church = { city: 'Austin', zip: '78701' };
  const pageSize = 25;
  await page.goto('/churches/new');
  await page.locator('#city').fill('Phoenix');
  await expect(page.locator('#result-count'), 'the count must render').toHaveAttribute('data-total-count', String(pageSize));
  await expect(page).toHaveURL('/churches/created');
  const firstRow = rows[0];
  if (typeof value !== 'string') { throw new Error('the seeded value must be text'); }
  const bounded = randomInt(1, 100);
  test.each(churchNames)('renders %s on the card', async ({ page }) => {});
  await clickById(page, 'church-save-button');
  await clickById(page, 'church-name-field', 'Dallas');
  const cells = fixture.debugElement.queryAll(By.css('tbody tr'));
  vi.stubGlobal('fetch', fetchMock);
  vi.stubGlobal('fetch', 'not-a-function');
  const address = graph['address'];
  const tags = ['Gospel'];
  httpMock.expectOne('/api/churches');
  const options = fixture.nativeElement.querySelectorAll<HTMLOptionElement>('#church-page-size option');
  const box = await boxOf(page, '#church-title-0');
  const labelledBox = await boxOf(page, 'Tulsa');
  const descendantBox = await boxOf(page, '#church-list a');
  Object.defineProperty(navigator, 'geolocation', { value: geoMock });
  const shade = styleOf(page, '--color-focus');
  await page.goto(`${CHURCHES_URL}?q=Church&pageSize=20`);
  const renderedPage = `page ${index} of "Tempe"`;
  const templatePageSize = 40;
  assert.match(churchId, /^[0-9a-f]{8}-[0-9a-f]{4}$/);
  const pageRatio = 12 / 5;
  console.log('church fixtures: every seeded row reads back');
  type ScheduleKey = Pick<ServiceSchedule, 'id'>;
  const scheduleRecord = { name: 'Vespers' };
  await expect(page.locator('#church-link')).toHaveAttribute(
    'href',
    `/churches/${churchId}`,
  );
  await page.locator('#church-name').fill(
    'Norman',
  );
  page.on('console', message => messages.push(message));
  await page.waitForLoadState('networkidle');
  await page.goto(CHURCHES_URL, { waitUntil: 'domcontentloaded' });
  await page.keyboard.press('Enter');
  await context.grantPermissions(['geolocation']);
  const hasError = msg.type() === 'error';
  const display = await computedStyleOf(page, churchCardId, 'display');
  const radixed = parseInt(rawCount, 10);
  const parsedTwice = parseInt(rawCount) * 7;
  const settingsUrl = new URL('./e2e-settings.json', import.meta.url);
  const pagesUrl = new URL('../src/pages', import.meta.url);
  const elsewhereUrl = new URL('./fixture-page.json', pageBase);
  const rootedUrl = new URL('/absolute/rooted-page', import.meta.url);
  const barePath = './bare-settings.json';
  res.status(404).json({ detail: missingDetail });
  expect(response.status).toBe(401);
  test.skip(!process.env.WalkerBaseUrl, 'needs a deployed stack to walk');
  test.skip(isMobile, 'needs a wide viewport', 'Portland');
});

Given('a member with a confirmed account', async ({ page }) => {});
setGiven('a lookalike step pattern', async ({ page }) => {});
'@

Set-Content -LiteralPath (Join-Path $e2eDir 'magic-literals.spec.ts') -Value $magicLiteralSpec -Encoding UTF8
$sharedGeneratorSpec = @'
import { expect, test } from '@playwright/test';
import { lowercaseToken, newText } from '@crgolden/modules/testing';

function localToken(length: number): string {
  return 'x'.repeat(length);
}

test('renders the seeded church name', async ({ page }) => {
  const churchName = newText();
  const shortWord = lowercaseToken(8);
  const localWord = localToken(9);
  const stretched = lowercaseToken(8).length + 12;
  const typedName = 'Wichita';
  await expect(page.locator('#church-name')).toHaveText(churchName);
  await expect(page.locator('#church-city')).toHaveText(typedName);
});
'@
Set-Content -LiteralPath (Join-Path $e2eDir 'shared-generator.spec.ts') -Value $sharedGeneratorSpec -Encoding UTF8
$apiMemberSpec = @'
import { expect, test, vi } from 'vitest';

test('names API members, not data', () => {
  const spy = vi.spyOn(service, 'loadChurches');
  fixture.componentRef.setInput('items', rows);
  const holder = document.createElement('div');
  const spied = vi.spyOn(service, 'loadChurches').mockReturnValue('Tulsa');
  const navigate = vi.spyOn(TestBed.inject(Router), 'navigate');
  expect(next?.tagName, 'a page turn is a link').toBe('BUTTON');
  expect(offset).toBe('0');
  expect(
    compiled.textContent,
    'the wrapped message explains the pin',
  ).toContain(expected);
  expect(
    other,
    'a message built '
      + 'across two lines',
  ).toBe(expected);
  expect(list).toEqual([
    first,
    'Waco',
  ]);
  process.stdout.write('the suite finished its checks\n');
  assert.ok(ready, 'the pipeline never reported ready');
  assert.ok(lookup('Killeen'));
  const rows = prefixLocator(page, 'library-row-');
  const pickedTile = await pickFromPrefix(page, 'catalog-tile-');
  const cityRows = prefixLocator(locatorFor('Temple'), prefix);
  expect(progress).toContain('0%');
  expect(progress).toContain('7%');
});

@Component({
  selector: 'app-host-fixture',
  template: `
    <h3 class="item-heading">First question?</h3>
    <p>Tyler</p>
  `,
  providers: ['Lubbock'],
})
class HostFixture {}

it.each([
  [first, second],
  [third, fourth],
])('renders every row in %s', (value) => {
  const city = 'Denton';
  expect(render(value, city)).toBeTruthy();
});
'@
Set-Content -LiteralPath (Join-Path $e2eDir 'api-members.spec.ts') -Value $apiMemberSpec -Encoding UTF8
$messageShapesSpec = @'
import { expect, test } from '@playwright/test';

test('explains every failure in words', async ({ page }) => {
  expect(
    measured,
    `the list (${measured}px) must fit, or its ` +
      'content spills past the bottom edge',
  ).toBeLessThanOrEqual(limit);
  if (!listed.ok()) {
    throw new Error(
      `could not list: ${listed.status()}. ` +
        'A sweep that cannot list reads as one with nothing to do.',
    );
  }
  await expect
    .poll(() => count(), {
      message: 'the frame neither posted its outcome nor was refused',
    })
    .toBe(0);
  await expect.poll(() => count(), { message: 'the inline poll message explains itself' }).toBe(0);
  const payload = { message: 'Galveston' };
  const label = prefix +
    'Amarillo';
  setup('Odessa');
});
'@
Set-Content -LiteralPath (Join-Path $e2eDir 'message-shapes.spec.ts') -Value $messageShapesSpec -Encoding UTF8
$titleAliasSetup = @'
import { test as setup, expect } from '@playwright/test';

setup('signs in through the BFF and saves the session', async ({ page }) => {
  await page.locator('#email').fill('Midland');
});
'@
Set-Content -LiteralPath (Join-Path $e2eDir 'title-alias.setup.ts') -Value $titleAliasSetup -Encoding UTF8
$ruleTesterSpec = @'
import { RuleTester } from 'eslint';

const ruleTester = new RuleTester({ languageOptions: { ecmaVersion: 2022, sourceType: 'module' } });
const unrelatedCity = 'Abilene';
const SCAFFOLD_HEADER = 'class Host {\n';

function wrapped(body: string): string {
  return SCAFFOLD_HEADER + body;
}

ruleTester.run('no-click-navigation', rule, {
  valid: [{ name: 'a link navigates', code: 'const link = routerLink;' }],
  invalid: [{ code: 'router.navigate(target);', options: [{ navigationMethods: ['navigate'] }], errors: 1 }],
});
'@
Set-Content -LiteralPath (Join-Path $e2eDir 'rule-tester.spec.ts') -Value $ruleTesterSpec -Encoding UTF8
$syntheticDir = Join-Path $e2eDir 'synthetic'
New-Item -ItemType Directory -Force -Path $syntheticDir | Out-Null
$walkerActions = @'
export const ACTIONS = [
  {
    name: 'search by keyword',
    weight: 5,
    run: async (page) => page.locator('#church-search').fill('grace'),
  },
];
'@
Set-Content -LiteralPath (Join-Path $syntheticDir 'actions.ts') -Value $walkerActions -Encoding UTF8
Set-Content -LiteralPath (Join-Path $e2eDir 'worker-library-constants.ts') -Value $vendorConstantsTypeScript -Encoding UTF8
Set-Content -LiteralPath (Join-Path $e2eDir 'lookalike-constants.ts') -Value $lookalikeConstantsTypeScript -Encoding UTF8
Set-Content -LiteralPath (Join-Path $unitDir 'WidgetLibraryConstants.cs') -Value $vendorConstantsCSharp -Encoding UTF8
Set-Content -LiteralPath (Join-Path $pySourceDir 'copy_source.py') -Value $copySourcePython -Encoding UTF8
Set-Content -LiteralPath (Join-Path $pyTestsDir 'test_copy_source.py') -Value $copyTestsPython -Encoding UTF8
$tsLogicSpec = @'
function pickBranch(flag: boolean): number {
  if (flag) {
    return 1;
  }
  return 2;
}

test.describe('logic', () => {
  test.beforeEach(() => {
    if (pickBranch(false) === 9) {
      throw new Error('hook');
    }
  });

  it.each([{ size: 3 }, { size: 4 }])('reads a table row as data, not as the body', ({ size }) => {
    expect(size).toBeGreaterThan(0);
  });

  it.each([
    {
      width: 10,
    },
  ])('walks past a multi-line row into the callback', ({ width }) => {
    if (width === 11) {
      expect(width).toBe(11);
    }
  });

  it('branches inside a test', () => {
    const picked = pickBranch(true);
    if (picked === 5) {
      expect(picked).toBe(5);
    } else {
      expect(picked).toBe(1);
    }
    for (const step of [6, 7]) {
      expect(step).toBeGreaterThan(0);
    }
    switch (picked) {
      default:
        break;
    }
    try {
      pickBranch(false);
    } catch (thrown) {
      expect(thrown).toBeUndefined();
    }
    const fallback = picked ?? 8;
    const shown = picked > 0 ? 'positive' : 'other';
    expect([fallback, shown].map((value) => value)).toHaveLength(2);
  });
});
'@
Set-Content -LiteralPath (Join-Path $prodDir 'logic.spec.ts') -Value $tsLogicSpec -Encoding UTF8
$pyLogicTests = @'
import pytest


def pick_branch(flag):
    if flag:
        return 1
    return 2


def test_branches():
    """A docstring that mentions if and for in prose."""
    picked = pick_branch(True)
    if picked == 1:
        assert picked == 1
    elif picked == 3:
        assert picked == 3
    else:
        assert picked == 2
    for step in [1, 2]:
        assert step > 0
    for spread in (
        picked,
    ):
        assert spread
    while picked > 5:
        picked -= 1
    try:
        pick_branch(False)
    except ValueError:
        pass
    match picked:
        case _:
            pass
    doubled = [step * 2 for step in [1, 2]]
    assert any(
        step > 1
        for step in doubled
        if step
    )
    label = 1 if picked else 2
    assert label


def test_raises():
    with pytest.raises(ValueError):
        int(None)
'@
Set-Content -LiteralPath (Join-Path $pyTestsDir 'test_logic.py') -Value $pyLogicTests -Encoding UTF8
$exemptToolsDir = Join-Path $fixtureRoot 'Tools\Claude'
$exemptPsnpyDir = Join-Path $fixtureRoot 'psnpy\tests'
foreach ($d in @($exemptToolsDir, $exemptPsnpyDir)) { New-Item -ItemType Directory -Path $d -Force | Out-Null }
Set-Content -LiteralPath (Join-Path $exemptToolsDir 'ExemptToolsFixture.cs') -Value @'
namespace Tools;

public static class ExemptToolsFixture
{
    public static string Missing() => string.Empty;
}
'@ -Encoding UTF8
Set-Content -LiteralPath (Join-Path $exemptPsnpyDir 'test_exempt_psnpy.py') -Value @'
def test_exempt_branches():
    picked = 1
    if picked == 1:
        assert picked
'@ -Encoding UTF8
$pyOracleConstants = @'
EXPECTED_WIDGET_EVENTS = {
    "Worker pipeline ready",
}
'@
Set-Content -LiteralPath (Join-Path $pyTestsDir 'widget_constants.py') -Value $pyOracleConstants -Encoding UTF8
$pyLookalikeConstants = @'
EXPECTED_WIDGET_EVENTS = {
    "Worker pipeline ready",
}


def events():
    return EXPECTED_WIDGET_EVENTS
'@
Set-Content -LiteralPath (Join-Path $pyTestsDir 'lookalike_widget_constants.py') -Value $pyLookalikeConstants -Encoding UTF8

$styleDir = Join-Path $fixtureRoot 'FixtureApp\src'
$vendorStyleDir = Join-Path $fixtureRoot 'FixtureApp\FixtureApp\wwwroot\lib\vendor'
foreach ($d in @($styleDir, $vendorStyleDir)) { New-Item -ItemType Directory -Path $d -Force | Out-Null }
$globalStylesheet = @'
@import 'tailwindcss';
@plugin "daisyui/theme" {
  --color-plugin-token: #111111;
}
@theme {
  --color-canvas: oklch(0.18 0.012 265);
  --color-rebound: oklch(0.5 0 0);
}
@font-face {
  font-family: 'Fixture Sans';
}
@media (prefers-color-scheme: light) {
  :root {
    --color-canvas: oklch(0.97 0.004 265);
    --color-minted-here: oklch(0.9 0 0);
    --color-rebound: var(--color-canvas);
  }
}
:root {
  --space-fixture: 0.25rem;
}
@layer base {
  h2 {
    font-family: var(--font-heading-fixture);
  }
  input[type='search']:focus {
    border-color: var(--color-canvas);
  }
  a:not(.plain-link) {
    color: var(--color-link-fixture);
  }
  *::before {
    box-sizing: border-box;
  }
  nav a {
    color: var(--color-nav-fixture);
  }
  .base-class-fixture {
    margin: 0;
  }
  q::after {
    content: '}';
    color: var(--quoted-fixture-color);
  }
}
@layer components {
  .btn-fixture {
    display: inline-flex;
  }
}
@utility z-fixture {
  z-index: 10;
}
@keyframes fixture-spin {
  to {
    transform: rotate(360deg);
  }
}
/* a { color: red; }
   commented-out-fixture */
.after-comment-fixture { opacity: 0.5; }
'@
Set-Content -LiteralPath (Join-Path $styleDir 'fixture-styles.css') -Value $globalStylesheet -Encoding UTF8
$componentStylesheet = @'
.fixture-card {
  padding: 1rem;
}
'@
Set-Content -LiteralPath (Join-Path $styleDir 'fixture.component.css') -Value $componentStylesheet -Encoding UTF8
$markupWithStyles = @'
<div class="card" style="max-height: 320px;"></div>
<img [style.width.px]="size" alt="bound">
<span [attr.style]="dynamicStyle">bound attr</span>
<p style="">empty style fixture</p>
<div data-style="compact">data attribute fixture</div>
<style>
  .inline-block-fixture {
    color: red;
  }
</style>
'@
Set-Content -LiteralPath (Join-Path $styleDir 'fixture-page.html') -Value $markupWithStyles -Encoding UTF8
$astroWithStyles = @'
<div style={dynamic}>astro dynamic fixture</div>
<p style="margin-top: 2rem">astro static fixture</p>
'@
Set-Content -LiteralPath (Join-Path $styleDir 'FixtureCard.astro') -Value $astroWithStyles -Encoding UTF8
Set-Content -LiteralPath (Join-Path $styleDir 'FixtureView.cshtml') -Value '<ul style="overflow-y:auto">razor fixture</ul>' -Encoding UTF8
Set-Content -LiteralPath (Join-Path $vendorStyleDir 'vendor-fixture.css') -Value '.vendor { color: blue; }' -Encoding UTF8
$strykerReportDir = Join-Path $fixtureRoot 'FixtureApp\FixtureApp\StrykerOutput\run\reports'
New-Item -ItemType Directory -Path $strykerReportDir -Force | Out-Null
Set-Content -LiteralPath (Join-Path $strykerReportDir 'stryker-fixture-report.html') -Value '<div style="box-shadow: none">stryker report fixture</div>' -Encoding UTF8
$cucumberReportDir = Join-Path $fixtureRoot 'FixtureApp\cucumber-report'
$cucumberLookalikeDir = Join-Path $fixtureRoot 'FixtureApp\cucumber-reporting'
New-Item -ItemType Directory -Path $cucumberReportDir, $cucumberLookalikeDir -Force | Out-Null
Set-Content -LiteralPath (Join-Path $cucumberReportDir 'cucumber-fixture-report.html') -Value '<div style="box-shadow: none">cucumber report fixture</div>' -Encoding UTF8
$bddGenDir = Join-Path $fixtureRoot 'FixtureApp\e2e\.features-gen\chromium'
$bddGenLookalikeDir = Join-Path $fixtureRoot 'FixtureApp\e2e\features-gen-notes'
New-Item -ItemType Directory -Path $bddGenDir, $bddGenLookalikeDir -Force | Out-Null
Set-Content -LiteralPath (Join-Path $bddGenDir 'bddgen-fixture.spec.ts') -Value "test('generated', async ({ page }) => { await page.locator('#city').fill('Tulsa'); });" -Encoding UTF8
Set-Content -LiteralPath (Join-Path $bddGenLookalikeDir 'bddgen-lookalike.spec.ts') -Value "test('authored', async ({ page }) => { await page.locator('#city').fill('Tempe'); });" -Encoding UTF8
Set-Content -LiteralPath (Join-Path $cucumberLookalikeDir 'cucumber-lookalike.html') -Value '<div style="box-shadow: none">cucumber lookalike fixture</div>' -Encoding UTF8
$inlineStylesComponent = @'
@Component({
  selector: 'app-inline-styles',
  styles: ['.x { color: red; }'],
  styleUrl: './inline-styles.component.css',
})
export class InlineStylesComponent {}
'@
Set-Content -LiteralPath (Join-Path $styleDir 'inline-styles.component.ts') -Value $inlineStylesComponent -Encoding UTF8

Write-Host "Fixture workspace: $fixtureRoot"
& $scriptUnderTest -WorkspaceRoot $fixtureRoot -OutputPath $csvPath | Out-Null

$rows = @(Import-Csv -LiteralPath $csvPath)
Write-Host "Catalog produced $($rows.Count) rows against the fixture workspace.`n"

$failures = New-Object System.Collections.Generic.List[string]
$rowsByFile = @{}
foreach ($row in $rows) {
    $name = [System.IO.Path]::GetFileName($row.Path)
    if (-not $rowsByFile.ContainsKey($name)) { $rowsByFile[$name] = New-Object System.Collections.Generic.List[object] }
    $rowsByFile[$name].Add($row)
}
$fixturePaths = @{}
$fixtureLines = @{}

function Get-FixtureRows([string]$fileName) {
    if ($script:rowsByFile.ContainsKey($fileName)) { return $script:rowsByFile[$fileName] }
    return @()
}

function Get-FixtureLine([string]$fileName, [string]$needle) {
    if (-not $script:fixturePaths.ContainsKey($fileName)) {
        $script:fixturePaths[$fileName] = (Get-ChildItem -Path $fixtureRoot -Recurse -Filter $fileName | Select-Object -First 1).FullName
    }
    $fullName = $script:fixturePaths[$fileName]
    # `@()` because Get-Content returns a one-line file as a bare string, and indexing a string yields its
    # characters, so a one-line fixture could never match its needle.
    if (-not $script:fixtureLines.ContainsKey($fullName)) { $script:fixtureLines[$fullName] = @(Get-Content -LiteralPath $fullName) }
    $lines = $script:fixtureLines[$fullName]
    for ($i = 0; $i -lt $lines.Count; $i++) {
        if ($lines[$i].Contains($needle, [System.StringComparison]::OrdinalIgnoreCase)) { return $i + 1 }
    }
    throw "Fixture '$fileName' has no line containing '$needle' - the harness fixture and its assertions have drifted apart."
}

function Assert-Row([string]$label, [string]$fileName, [string]$needle, [int]$rule, [string]$disposition) {
    $line = Get-FixtureLine $fileName $needle
    $hit = Get-FixtureRows $fileName | Where-Object {
        [int]$_.Line -eq $line -and [int]$_.Rule -eq $rule -and ($disposition -eq '' -or $_.Disposition -eq $disposition)
    }
    if ($hit) {
        Write-Host "  PASS  $label"
    }
    else {
        $script:failures.Add("MUST-FIRE  $label (expected rule $rule$(if ($disposition) { "/$disposition" }) at ${fileName}:$line)")
        Write-Host "  FAIL  $label" -ForegroundColor Red
    }
}

# Assert-NoRow cannot express "this line reports, but NOT under that disposition", which is exactly what a
# line carrying a payload needs: it must produce its one needs-triage row and must NOT also produce
# generate rows from the payload's insides. Without this the suppression could only be asserted by its
# absence, and a line that reported both would pass.
function Assert-NoRowWithDisposition([string]$label, [string]$fileName, [string]$needle, [int]$rule, [string]$disposition) {
    $line = Get-FixtureLine $fileName $needle
    $hit = Get-FixtureRows $fileName | Where-Object {
        [int]$_.Line -eq $line -and [int]$_.Rule -eq $rule -and $_.Disposition -eq $disposition
    }
    if ($hit) {
        $script:failures.Add("MUST-NOT-FIRE  $label (rule $rule/$disposition fired at ${fileName}:$line, category '$($hit[0].Category)')")
        Write-Host "  FAIL  $label" -ForegroundColor Red
    }
    else {
        Write-Host "  PASS  $label"
    }
}

function Assert-NoRow([string]$label, [string]$fileName, [string]$needle, [int]$rule) {
    $line = Get-FixtureLine $fileName $needle
    $hit = Get-FixtureRows $fileName | Where-Object {
        [int]$_.Line -eq $line -and [int]$_.Rule -eq $rule
    }
    if ($hit) {
        $script:failures.Add("MUST-NOT-FIRE  $label (rule $rule fired at ${fileName}:$line, disposition '$($hit[0].Disposition)')")
        Write-Host "  FAIL  $label" -ForegroundColor Red
    }
    else {
        Write-Host "  PASS  $label"
    }
}

Write-Host "Rule 1 - no empty strings"
Assert-Row   'flags ?? string.Empty' 'ProdFixture.cs' '?? string.Empty' 1 'use-null'
# An empty string handed to a call is a VALUE unless the callee is a separator API.
Assert-Row   'flags an empty string passed as a value to a factory' 'ProdFixture.cs' 'EveryProvider(string.Empty)' 1 'use-null'
Assert-Row   'flags an empty string added to a list' 'ProdFixture.cs' 'roleRows.Add(string.Empty)' 1 'use-null'
Assert-NoRow 'ignores an empty string as the Replace separator' 'ProdFixture.cs' 'spaced.Replace(' 1
Assert-NoRow 'ignores an empty string as the Join separator' 'ProdFixture.cs' 'string.Join(string.Empty, partsToGlue)' 1
# ASP.NET's model-level error key is the empty string, and null throws there, so the framework mandates it.
Assert-NoRow 'ignores the empty model-level key of AddModelError' 'ProdFixture.cs' 'AddModelError(string.Empty, failureText)' 1
# The control: the same callee with an empty MESSAGE is an ordinary blank value and still reports.
Assert-Row   'still reports an empty message passed to AddModelError' 'ProdFixture.cs' 'AddModelError(propertyName, string.Empty)' 1 'use-null'
Assert-NoRow 'ignores a parenthesis inside a non-empty literal' 'ProdFixture.cs' 'Wrap("(x)", bracketValue)' 1
Assert-NoRow 'ignores an empty string a test feeds the code under test' 'FixtureTests.cs' 'Verifier.Verify(string.Empty)' 1
Assert-Row   'flags an empty string assigned in a one-line object initializer' 'FixtureTests.cs' '{ Label = string.Empty }' 1 'use-null'
Assert-Row   'flags return string.Empty;' 'ProdFixture.cs' 'return string.Empty;' 1 'use-null'
Assert-Row   'flags an expression-bodied => string.Empty' 'ProdFixture.cs' 'Blank() => string.Empty' 1 'use-null'
Assert-Row   'flags return "";' 'ProdFixture.cs' 'return "";' 1 'use-null'
# `global using static System.String` makes bare `Empty` mean `string.Empty`, and Identity's
# Properties/GlobalUsings.cs:16 has exactly that: `public string ClientId { get; set; } = Empty;` in
# Pages/Account/Manage/Grants.cshtml.cs is a rule-1 violation this detector could not see, because it
# matched only the qualified spelling. Rule 5 already handles the same using-static form for bare
# `IsNullOrEmpty(`; rule 1 did not, which is the inconsistency. The three controls below are what keep
# the widening off everything else named Empty: a `.` before it means someone else's member.
Assert-Row   'flags a bare Empty assignment via using static' 'ProdFixture.cs' '= Empty;' 1 'use-null'
Assert-Row   'flags a bare Empty return' 'ProdFixture.cs' 'return Empty;' 1 'use-null'
Assert-Row   'flags a bare Empty coalesce' 'ProdFixture.cs' 'input ?? Empty;' 1 'use-null'
Assert-NoRow 'ignores Array.Empty<string>()' 'ProdFixture.cs' 'Array.Empty<string>()' 1
Assert-NoRow 'ignores ImmutableArray<int>.Empty' 'ProdFixture.cs' 'ImmutableArray<int>.Empty' 1
Assert-NoRow 'ignores a member merely starting with Empty' 'ProdFixture.cs' 'EmptyLabelIsNotTheKeyword' 1

Write-Host "Rule 1 - TypeScript, armed via Scan-TypeScriptLine"
# Loud-on-zero guard. A broken .ts dispatch arm makes every per-line assertion below "pass" as a
# MUST-NOT-FIRE and the whole language reads as clean, which is the 0-violations-where-there-were-33
# failure mode. Count MATCHED FILES before trusting matched lines.
$tsRows = @($rows | Where-Object { $_.Path -like '*prod.ts' })
if ($tsRows.Count -eq 0) {
    $failures.Add('DISPATCH  prod.ts produced ZERO rows - TypeScript is not being scanned at all, so every MUST-NOT-FIRE below is vacuous.')
    Write-Host '  FAIL  prod.ts produced at least one rule-1 row' -ForegroundColor Red
}
else {
    Write-Host "  PASS  prod.ts produced at least one rule-1 row ($($tsRows.Count) rows)"
}
Assert-Row   'flags an empty-string return' 'prod.ts' "    return '';" 1 'use-null'
Assert-Row   'flags ?? empty string' 'prod.ts' "value ?? ''" 1 'use-null'
# A default that binds the empty string is rule 1's shape, same as the Angular `@Input() alt = ''`
# the real-tree pass found. Stated as a MUST-FIRE so the reading is explicit rather than incidental.
Assert-Row   'flags a default parameter bound to an empty string' 'prod.ts' "suffix = ''" 1 'use-null'
# The four idiomatic JS forms. Each is an ARGUMENT to a call, not a bound value or a return - "no
# separator" spelled the only way JavaScript spells it. Flagging them sends someone to break working code.
Assert-NoRow 'ignores parts.join()' 'prod.ts' "parts.join('')" 1
Assert-NoRow 'ignores word.split()' 'prod.ts' "word.split('')" 1
Assert-NoRow 'ignores replace(x, empty)' 'prod.ts' "base.replace(" 1
Assert-NoRow 'ignores an empty template literal seed argument' 'prod.ts' 'parts.reduce(' 1
# Comparing against the empty string is rule 8's shape; rule 1 is about PRODUCING one.
Assert-NoRow 'ignores === against an empty string' 'prod.ts' "value === ''" 1
# Pins Remove-TsLiteralBodies: the four MUST-FIREs above still fire, and prose that merely spells `?? ''`
# inside a string does not.
Assert-NoRow 'ignores an empty string spelled inside a string literal' 'prod.ts' 'quotedLiteral' 1
Assert-NoRow 'ignores a non-empty string assigned' 'prod.ts' "nonEmptyAssigned = 'Top level'" 1
Assert-NoRow 'ignores a non-empty template returned' 'prod.ts' 'return `node-${nodeId}`;' 1

Write-Host "Rule 2 - strongest type, and where it stops"
Assert-Row   'flags a string property named *Url' 'ProdFixture.cs' 'public string? ManualUrl' 2 'use-uri'
# Every other string property reports only when production code converts it: that conversion is the
# evidence a stronger type exists. A name ending in Id or Type is not evidence, so an identifier someone
# else mints stays a string.
Assert-Row   'flags a string property production parses as a Guid' 'ProdFixture.cs' 'public string? ParsedRunId' 2 'use-guid'
Assert-Row   'flags a string property production throws-parses as a timestamp, through ?.' 'ProdFixture.cs' 'public string? ParsedReleaseStamp' 2 'use-datetimeoffset'
Assert-NoRow 'ignores a string property only a TryParse reads (a boundary)' 'ProdFixture.cs' 'public string? BoundaryStamp' 2
Assert-NoRow 'ignores a string parameter only a TryParse reads (a boundary)' 'ProdFixture.cs' 'ReadCsvLatitude(string? csvLatitude)' 2
Assert-NoRow 'ignores an Id-named string property nothing converts' 'ProdFixture.cs' 'public string? OpaqueEntitlementId' 2
Assert-NoRow 'ignores a property only a test converts' 'ProdFixture.cs' 'public string? OpaqueConceptType' 2
Assert-NoRow 'ignores a property whose name matches only a parsed local' 'ProdFixture.cs' 'public string? LocallyParsedToken' 2
Assert-NoRow 'ignores a same-named property in a repo that never converts it' 'PlainOptions.cs' 'public string? ParsedRunId' 2
# The same evidence for a string parameter or local: the file passes it, bare, to a conversion.
Assert-Row   'flags a string parameter the same file parses as a Guid' 'ProdFixture.cs' 'RecordOwner(string ownerSub)' 2 'use-guid'
Assert-NoRow 'ignores a string parameter nothing converts' 'ProdFixture.cs' 'MeasureHandle(string opaqueHandle)' 2
Assert-NoRow 'ignores a string parameter whose name only a lambda elsewhere converts' 'ProdFixture.cs' 'PadFlag(string flagValue)' 2
Assert-Row   'still flags a string parameter the file parses, beside a lambda reusing its name' 'ProdFixture.cs' 'ReadCount(string countText)' 2 'use-guid'
Assert-NoRow 'ignores a string type pattern in a switch arm' 'ProdFixture.cs' 'string cellText =>' 2
Assert-NoRow 'ignores a same-named parameter in a file that never converts it' 'EdmIgnoredRequestFixture.cs' 'KeepOwner(string ownerSub)' 2
Assert-NoRow 'ignores a string parameter only a test parses' 'FixtureTests.cs' 'ReadSubInATest(string testOwnerSub)' 2
# The INVERSE, and the one that shipped: Uri on a serialization contract becomes an EDM complex type
# carrying only Segments, so it is lossy on read and undeserializable on write. The detector cannot tell
# an EDM entity from a plain POCO, so this is needs-triage rather than a violation - but it must fire, or
# the next Uri property on an OData entity 400s every PATCH again with a green unit suite.
Assert-Row   'flags a Uri auto-property' 'ProdFixture.cs' 'public Uri? SchemaDeclaredUri' 2 'needs-triage'
# Rule 2 is about the declared shape of a stored/serialized member. A Uri flowing through a signature or a
# local is the type doing its job, and flagging it would bury the property rows in noise.
Assert-NoRow 'ignores a Uri return type and parameter' 'ProdFixture.cs' 'public Uri BuildEndpoint' 2
Assert-NoRow 'ignores a Uri local' 'ProdFixture.cs' 'var composed = new Uri' 2
# The inverse arm is armed per repo by an OData package reference; PlainApp has none.
Assert-NoRow 'ignores a Uri auto-property in a repo without OData' 'PlainOptions.cs' 'public Uri? VerifyEndpoint' 2
# The arm stops at the edge of the EDM. A type the model builder Ignore<>s is not in the model, so no PATCH
# can reach it as a complex type. SchemaDeclaredUri above is the control: same shape, same OData repo, not
# ignored, still reported - so this is the EDM boundary, not "any Uri in an OData repo is fine".
Assert-NoRow 'ignores a Uri property on a type Ignore<>d from the EDM' 'EdmIgnoredRequestFixture.cs' 'public Uri? ManualUrl' 2
# The *Url arm mirrors CA1056, so a repo .editorconfig scope silencing CA1056 silences it for the same paths.
Assert-NoRow 'honours an .editorconfig scope that silences CA1056' 'Login.cshtml.cs' 'public string? ReturnUrl' 2

Write-Host "Rule 3 - DateTimeOffset in UTC"
Assert-Row   'flags a DateTime property' 'ProdFixture.cs' 'public DateTime CreatedAt' 3 ''

Write-Host "Rule 4 - no nullable collections"
Assert-Row   'flags List<string>?' 'ProdFixture.cs' 'List<string>? Tags' 4 'use-empty-collection'
Assert-Row   'flags a nullable array' 'ProdFixture.cs' 'Aliases' 4 'use-empty-collection'
Assert-Row   'flags a nullable byte array' 'ProdFixture.cs' 'EncryptedKey' 4 'use-empty-collection'

Write-Host "Rule 5 - IsNullOrWhiteSpace"
Assert-Row   'flags string.IsNullOrEmpty' 'ProdFixture.cs' 'string.IsNullOrEmpty(value)' 5 'use-isnullorwhitespace'
Assert-NoRow 'ignores RedisValue.IsNullOrEmpty property' 'ProdFixture.cs' 'cached.IsNullOrEmpty' 5
# Regression, and the reason Remove-LineComment is no longer a plain `//.*$` replace: the `//` inside
# "http://" was read as the start of a comment, so the line was truncated there and EVERY rule went blind
# to the rest of it. This asserts rule 5 still sees a violation that sits after such a literal.
Assert-Row   'sees past a // inside a string literal' 'ProdFixture.cs' 'CommentStripperRegression' 5 'use-isnullorwhitespace'

Write-Host "Rule 8 - ordinal string comparison"
Assert-Row   'flags == against a literal' 'ProdFixture.cs' 'kind == "denomination"' 8 'use-string-equals-ordinal'
# The literal-equality arm used to read literal BODIES, so `$"=== {name} ==="` and a base64 literal ending
# in `==` both scored as string comparisons. The arm now reads the blanked line, where a real `== "x"`
# still leaves `== ""` behind and text inside a literal leaves nothing.
Assert-NoRow 'ignores == inside an interpolated string' 'ProdFixture.cs' 'Banner(string name)' 8
Assert-NoRow 'ignores a base64 literal ending in ==' 'ProdFixture.cs' 'Base64Vector' 8
# Culture-sensitive BY DEFAULT: a latent locale bug, not just an unstated contract.
Assert-Row   'flags StartsWith(string) with no comparison' 'ProdFixture.cs' 'StartsWith("Bearer ")' 8 'use-ordinal'
Assert-Row   'flags CompareTo, which has no overload to add' 'ProdFixture.cs' 'a.CompareTo(b)' 8 'use-string-compare-ordinal'
Assert-Row   'flags ToLower() case-folding' 'ProdFixture.cs' 'value.ToLower()' 8 'use-ordinalignorecase'
# Ordinal by default, contract unstated. These five production sites are exactly what CA1309 found in
# Identity, and what the old `== "`-only detector could never see.
Assert-Row   'flags two-arg string.Equals' 'ProdFixture.cs' 'string.Equals(a, b);' 8 'state-ordinal'
Assert-Row   'flags string.Contains(string) with no comparison' 'ProdFixture.cs' 'HasScheme' 8 'state-ordinal'
# Two receivers whose Contains is membership, not string search: DbParameterCollection.Contains(string)
# has no StringComparison overload, and xUnit's Assert.Contains(item, collection) is set membership (its
# string-in-string overload is CA1307's to catch, which the analyzers do and this text scan cannot).
Assert-NoRow 'ignores DbParameterCollection.Contains' 'ProdFixture.cs' 'ParameterBound' 8
Assert-NoRow 'ignores Assert.Contains membership' 'ProdFixture.cs' 'CollectionMembership' 8
# Undecidable without type information: LINQ-to-Objects is CurrentCulture, the same line over an EF
# IQueryable is database collation, and the element may not be a string at all.
Assert-Row   'reports ordering without a comparer for triage' 'ProdFixture.cs' 'names.OrderBy(n => n);' 8 'needs-triage'
# The char overloads are ordinal ALREADY. Flagging them sends someone to "fix" correct code.
# The exemption is scoped to the CALL, not the line. A line-level test would let the compliant
# string.Equals hide the bare StartsWith beside it - meaning the detector would go blind precisely as
# adoption of the rule spread through a file.
Assert-Row   'still flags a bare call beside a compliant one' 'ProdFixture.cs' 'MixedCompliance' 8 'use-ordinal'
Assert-NoRow 'ignores the char overload StartsWith(char)' 'ProdFixture.cs' "StartsWith('{')" 8
Assert-NoRow 'ignores a call that states StringComparison' 'ProdFixture.cs' 'SchemeCheck' 8
Assert-NoRow 'ignores string.Equals with StringComparison' 'ProdFixture.cs' 'ExplicitEquals' 8
Assert-NoRow 'ignores OrderBy with a StringComparer' 'ProdFixture.cs' 'SortedExplicitly' 8

# A wrapped argument list is still one call. Before the balance scan followed the call past the end of the
# line, every Allman-wrapped string.Equals reported state-ordinal against code that already states the
# comparison - reproducible on every run, and re-dismissed by hand every time.
Assert-NoRow 'ignores string.Equals stating the comparison on a later line' 'ProdFixture.cs' 'WrappedOrdinal' 8
# The control: the same wrapped shape WITHOUT a comparison must still report, or the lookahead has simply
# gone blind to multi-line calls.
Assert-Row   'still flags a wrapped string.Equals with no comparison' 'ProdFixture.cs' 'WrappedWithoutComparison' 8 'state-ordinal'
# An equality operator handed EqualityComparer<T>.Default has stated its comparer, whatever T is; before this
# the word list knew only the string-specific spellings, so an enum-keyed ToDictionary that states it reported.
Assert-NoRow 'ignores a wrapped ToDictionary supplying an EqualityComparer' 'ProdFixture.cs' 'KeyedWithEqualityComparer' 8
# The control: the same wrapped ToDictionary without a comparer must still report for triage.
Assert-Row   'still reports a wrapped ToDictionary with no comparer' 'ProdFixture.cs' 'KeyedWithoutComparer' 8 'needs-triage'
# An ordering handed Comparer<T>.Default for a non-string T has stated its comparer; the culture default
# cannot bite an int.
Assert-NoRow 'ignores an OrderBy supplying Comparer<int>' 'ProdFixture.cs' 'RankedWithIntComparer' 8
# Comparer<string>.Default IS the culture-sensitive default, so stating it states the defect. Both spellings
# must still report.
Assert-Row   'still reports an OrderBy supplying Comparer<string>' 'ProdFixture.cs' 'RankedWithStringComparer' 8 'needs-triage'
Assert-Row   'still reports an OrderBy supplying Comparer<System.String>' 'ProdFixture.cs' 'RankedWithQualifiedStringComparer' 8 'needs-triage'

# EF Core translates these to SQL, where every rule 8 fix is untranslatable and throws at execution.
Assert-NoRow 'ignores a literal comparison inside an EF predicate' 'ProdFixture.cs' 'EfLiteralPredicate' 8
Assert-NoRow 'ignores ordering over a DbContext queryable' 'ProdFixture.cs' 'EfOrdering' 8
# The receiver sits on the previous physical line; the scanner reads the statement so far.
Assert-NoRow 'ignores ordering on a continuation line of a DbContext query' 'ProdFixture.cs' '.OrderByDescending(c => c.Created)' 8
Assert-NoRow 'ignores ordering over an Identity manager queryable' 'ProdFixture.cs' 'IdentityManagerOrdering' 8
# The control for the EF arm: in-memory LINQ is where the culture default actually bites, and it must still
# report. If this ever goes quiet, Test-EfQueryable has widened into a carve-out.
Assert-Row   'still reports in-memory ordering without a comparer' 'ProdFixture.cs' 'names.OrderBy(n => n);' 8 'needs-triage'
# A key whose member name ends in a rule-2 non-string suffix (CreatedAt, MsrpPrice) is not a string
# ordering; the culture default cannot bite and there is no StringComparer to pass.
Assert-NoRow 'ignores ordering over a key named for a non-string type' 'ProdFixture.cs' 'OrderByDescending(c => c.CreatedAt)' 8
# A no-argument OrderBy cannot be LINQ - the operator requires a key selector. It is the OData options
# builder enabling $orderby, which sorts nothing and has no comparer to state. The two must-fire controls
# above (names.OrderBy(n => n) and the bare Distinct) are what keep this from silencing real orderings.
Assert-NoRow 'ignores a no-argument OrderBy on a fluent options builder' 'ProdFixture.cs' 'oDataOptions.Select().Filter().OrderBy()' 8

Write-Host "Rule 9 - no null-forgiving"
Assert-Row   'flags maybe!.Trim()' 'ProdFixture.cs' 'maybe!.Trim()' 9 'narrow-the-type'
Assert-NoRow 'ignores a != b' 'ProdFixture.cs' 'a != b' 9
# The shapes the `\w`-only pattern could not see. Without these the rule-9 arm was self-confirming:
# it exercised only the one form the regex already handled, so a zero-row scan proved nothing.
Assert-Row   'flags a bang on a call result' 'ProdFixture.cs' 'GetValueOrDefault("key")!' 9 'narrow-the-type'
# Needle is the method name, not `items[0]!` - Get-FixtureLine matches with -like, where [0] is a
# wildcard character class and would look for the literal text `items0!`.
Assert-Row   'flags a bang on an index result' 'ProdFixture.cs' 'IndexResultNullForgiving' 9 'narrow-the-type'
Assert-NoRow 'ignores != between two call results' 'ProdFixture.cs' 'NotEqualOnCallResult' 9

Write-Host "Rule 9 - TypeScript, armed via Scan-TypeScriptLine"
Assert-Row   'flags a bang on a call result' 'prod.ts' "params.get('id')!" 9 'narrow-the-type'
Assert-Row   'flags a bang on a property' 'prod.ts' 'response.body!' 9 'narrow-the-type'
Assert-Row   'flags a bang on an at() result' 'prod.ts' 'items.at(-1)!' 9 'narrow-the-type'
Assert-NoRow 'ignores a leading negation' 'prod.ts' 'negated' 9
Assert-NoRow 'ignores !==' 'prod.ts' 'a !== b' 9
Assert-NoRow 'ignores a negation inside a ternary' 'prod.ts' 'guarded' 9

Write-Host "Rule 10 - E2E selects by id"
Assert-Row   'separates third-party DOM' 'fixture.spec.ts' 'leaflet-marker-icon' 10 'third-party-dom'
Assert-NoRow 'ignores a module specifier that names a third-party library' 'fixture.spec.ts' "from './leaflet-constants.js'" 10
Assert-Row   'flags a positional selector' 'fixture.spec.ts' 'td:first-child' 10 'use-id'
Assert-NoRow 'ignores model-bound input[name=]' 'fixture.spec.ts' "input[name='Input.Email']" 10
Assert-NoRow 'ignores an id selector' 'fixture.spec.ts' 'church-title-0' 10
# Rule 11 is test DATA, not unit-test data: an E2E fixture's literal encodes a shape exactly as a unit
# spec's does. Get-Area reads the e2e segment before the project name, so a file's directory decides
# whether it is swept at all.
Assert-Row   'flags a magic literal in an E2E fixture' 'E2EDataFixtureTests.cs' '"Phoenix"' 11 'generate'
Assert-NoRow 'ignores a generated value in an E2E fixture' 'E2EDataFixtureTests.cs' 'Generated.NewCity()' 11

# The selector/route/wire-key positions, each with BOTH controls. Only the paired negative makes these arms
# a position classifier rather than area suppression: every "ignores" line below sits next to a "flags" line
# proving the same call, the same file and the same area still report the datum beside it.
Write-Host "Rule 11 - the E2E positions that name something outside the test"
Assert-NoRow 'ignores a selector argument' 'E2EDataFixtureTests.cs' 'ClickAsync("#save-button")' 11
Assert-Row   'flags the VALUE beside a selector' 'E2EDataFixtureTests.cs' '"#price-paid", "999"' 11 'generate'
Assert-NoRow 'ignores a navigation route' 'E2EDataFixtureTests.cs' 'GotoAsync("/admin/users")' 11
# A route that borrows the production folder constant instead of restating it. The two controls of opposite
# sign: a local hole borrows nothing, and a borrowed constant followed by `:` is a key, not a route.
Assert-NoRow 'ignores a route opening with a borrowed route constant' 'E2EDataFixtureTests.cs' 'AuthorizationNames.AdminFolder}/Clients/Edit' 11
Assert-Row   'flags a route opening with a local hole' 'E2EDataFixtureTests.cs' '{sectionFolder}/Roles/Edit' 11 'generate'
Assert-Row   'flags a borrowed constant that is not route-shaped' 'E2EDataFixtureTests.cs' 'page-three' 11 'generate'
# A Playwright route pattern is a glob, so it starts with `**/` rather than `/`. The cache-key row below
# is the control of opposite sign: a route-shape requirement that admits a glob must still reject a key.
Assert-NoRow 'ignores a glob route registration' 'E2EDataFixtureTests.cs' 'RouteAsync("**/products/api/**"' 11
Assert-NoRow 'ignores an HttpClient route' 'E2EDataFixtureTests.cs' 'PostAsJsonAsync("/churches"' 11
Assert-Row   'flags a route-shaped literal on a non-route call' 'E2EDataFixtureTests.cs' 'Describe("/not-a-route")' 11 'generate'
# The route wrapped onto its own line, and the REQUEST BODY wrapped onto the line after it. Only the route
# goes quiet: the body's literal is data and is what the must-fire control pins.
Assert-NoRow 'ignores a route wrapped onto its own line' 'E2EDataFixtureTests.cs' '"/chats/messages",' 11
Assert-Row   'flags a wrapped request body beside it' 'E2EDataFixtureTests.cs' 'ChatRequest("Where is the manual?")' 11 'generate'
# The same route slot reached through a constructor. Only the URI argument goes quiet; the body on the
# line below is data and is what the must-fire control pins.
Assert-NoRow 'ignores a route in an HttpRequestMessage' 'E2EDataFixtureTests.cs' 'HttpRequestMessage(HttpMethod.Post, "/chats/stream")' 11
Assert-Row   'flags the body beside a constructed request' 'E2EDataFixtureTests.cs' 'StringContent("streamed body")' 11 'generate'

# A multi-line verbatim string is a payload in another language, judged as a unit like a raw string. The
# two MUST-NOT-FIRE lines are the actual defect: before this, JavaScript `const` declarations inside a
# C# @"..." block were read as C# constants and reported as rule-11 rows against code this repo does not
# own. The single-line control proves the fix suppresses a BLOCK and not every verbatim string.
Assert-Row   'reports a multi-line verbatim block once at its opening' 'VerbatimBlockFixtureTests.cs' 'var script = @"' 11 'needs-triage'
Assert-NoRow 'does not sweep a JS const inside a verbatim block' 'VerbatimBlockFixtureTests.cs' 'const button = document.querySelector' 11
Assert-NoRow 'does not sweep a JS number inside a verbatim block' 'VerbatimBlockFixtureTests.cs' 'const attempts = 7;' 11
Assert-Row   'still sweeps a verbatim string that closes on its own line' 'VerbatimBlockFixtureTests.cs' 'still-swept-literal' 11 'generate'
Assert-NoRow 'ignores a URL matcher' 'E2EDataFixtureTests.cs' 'ToHaveURLAsync(new Regex(' 11
Assert-NoRow 'ignores a JSON property name' 'E2EDataFixtureTests.cs' 'GetProperty("slug")' 11
Assert-Row   'flags the same word outside GetProperty' 'E2EDataFixtureTests.cs' 'Lookup("slug")' 11 'generate'
Assert-NoRow 'ignores an xUnit skip reason' 'E2EDataFixtureTests.cs' 'Set NPSSO to run this probe' 11
# A separator argument carrying no letter or digit is punctuation; one carrying a word is still a datum.
Assert-NoRow 'ignores a punctuation separator argument' 'E2EDataFixtureTests.cs' 'Replace(" ", string.Empty' 11
Assert-Row   'flags a worded Replace argument' 'E2EDataFixtureTests.cs' 'Replace("Phoenix", city' 11 'generate'
# A header-name-only line, because the Add line below legitimately fires for its VALUE and so could
# never satisfy a no-row assertion.
Assert-NoRow 'ignores an HTTP header name' 'E2EDataFixtureTests.cs' 'Headers.Remove("x-psn-store-locale-override")' 11
# The header VALUE is data the test chose and must still report; without this the arm would be
# "any Headers.Add line is exempt" rather than "the name position is a wire contract".
Assert-Row   'flags the header VALUE beside the name' 'E2EDataFixtureTests.cs' '"apollo-require-preflight", "true"' 11 'generate'
Assert-Row   'flags a User-Agent value' 'E2EDataFixtureTests.cs' 'crgolden-probe/1.0' 11 'generate'
Assert-NoRow 'ignores a SQL parameter name' 'E2EDataFixtureTests.cs' 'AddWithValue("@Id"' 11
Assert-Row   'flags a parameter VALUE' 'E2EDataFixtureTests.cs' '"@City", "Tucson"' 11 'generate'
Assert-NoRow 'ignores an environment variable name' 'E2EDataFixtureTests.cs' 'GetEnvironmentVariable("CI_RUN_ID")' 11
Assert-Row   'flags an environment variable value' 'E2EDataFixtureTests.cs' '"CI_RUN_ID", "build-77"' 11 'generate'
Assert-NoRow 'ignores an attribute name ToHaveAttributeAsync reads' 'E2EDataFixtureTests.cs' '"data-error-code", expectedCode' 11
Assert-Row   'flags the attribute VALUE beside its name' 'E2EDataFixtureTests.cs' '"data-state", "suspended"' 11 'generate'
Assert-NoRow 'ignores a variable name read through EnvironmentSetting.Count' 'E2EDataFixtureTests.cs' 'Count("SYNTHETIC_STEPS"' 11
Assert-NoRow 'ignores a variable name read through EnvironmentSetting.Percent' 'E2EDataFixtureTests.cs' 'Percent("SYNTHETIC_SHARE"' 11
Assert-NoRow 'ignores a variable name read through EnvironmentSetting.Text' 'E2EDataFixtureTests.cs' 'Text("SYNTHETIC_LABEL"' 11
Assert-Row   'still flags the DEFAULT beside an EnvironmentSetting variable name' 'E2EDataFixtureTests.cs' '"walker-7"' 11 'generate'
Assert-NoRow 'ignores a const this file hands EnvironmentSetting as a default' 'E2EDataFixtureTests.cs' 'private const int DefaultSteps' 11
Assert-NoRow 'ignores a const this file hands EnvironmentSetting as a bound' 'E2EDataFixtureTests.cs' 'private const int MaxSteps' 11
Assert-Row   'still flags a const in that same file that EnvironmentSetting never reads' 'E2EDataFixtureTests.cs' 'private const int UnrelatedBudget' 11 'generate'
Assert-NoRow 'ignores a const this file hands GetEnvironmentVariable as the variable name' 'E2EDataFixtureTests.cs' 'private const string HeadedVariable' 11
Assert-Row   'still flags the value const compared with that variable on the same line' 'E2EDataFixtureTests.cs' 'private const string HeadedEnabledValue' 11 'generate'
Assert-NoRow 'ignores the category of a multi-line SuppressMessage' 'E2EDataFixtureTests.cs' '"FixtureDesign"' 11
Assert-NoRow 'ignores the check id of a multi-line SuppressMessage' 'E2EDataFixtureTests.cs' 'Fixture general catch' 11
Assert-NoRow 'ignores the justification of a multi-line SuppressMessage' 'E2EDataFixtureTests.cs' 'Fixture diagnostics never fail' 11
Assert-Row   'still flags a literal on the signature once the attribute has closed' 'E2EDataFixtureTests.cs' 'fixture-default-mode' 11 'generate'
Assert-NoRow 'ignores an inline script handed to EvaluateAsync' 'E2EDataFixtureTests.cs' 'document.body.id' 11
Assert-NoRow 'ignores a script wrapped onto the line after AddInitScriptAsync' 'E2EDataFixtureTests.cs' 'window.fixtureFlag' 11
Assert-NoRow 'ignores a const this file hands EvaluateAsync as its script' 'E2EDataFixtureTests.cs' 'private const string PageTitleScript' 11
Assert-NoRow 'ignores a wrapped const value this file hands WaitForFunctionAsync' 'E2EDataFixtureTests.cs' 'window.location.href' 11
Assert-Row   'still flags an argument handed TO the script' 'E2EDataFixtureTests.cs' 'fixture-script-argument' 11 'generate'
Assert-NoRow 'ignores a raw block opened as the script argument' 'E2EDataFixtureTests.cs' 'EvaluateHandleAsync("""' 11
Assert-NoRow 'ignores a verbatim block opened on the line after the call' 'E2EDataFixtureTests.cs' 'document.forms.length' 11
Assert-NoRow 'ignores an id selector, which rule 10 mandates' 'E2EDataFixtureTests.cs' '"#nav-home"' 11
Assert-NoRow 'ignores an id-prefix attribute selector' 'E2EDataFixtureTests.cs' 'admin-card-' 11
Assert-Row   'still flags a plain literal beside an id selector on the same line' 'E2EDataFixtureTests.cs' 'NavigateAction("home"' 11 'generate'
Assert-Row   'still flags a CLASS selector, which rule 10 rejects' 'E2EDataFixtureTests.cs' '".catalog-title"' 11 'generate'
Assert-NoRow 'ignores a const the language compels as a default parameter value' 'E2EDataFixtureTests.cs' 'private const string DefaultLinkPrefix' 11
Assert-NoRow 'ignores a case label' 'E2EDataFixtureTests.cs' 'case "GET"' 11
Assert-Row   'flags a verb held in a local' 'E2EDataFixtureTests.cs' 'fallbackVerb = "PATCH"' 11 'generate'
# The *Url receiver, the exact sibling of the *sql receiver arm. The SUFFIX is the anchor, so the same
# assertion against an ordinary string receiver still reports - that is the control, not the decoration.
Assert-NoRow 'ignores a route asserted against page.Url' 'E2EDataFixtureTests.cs' '"/Account/Login", page.Url' 11
Assert-Row   'flags the same shape on a non-URL receiver' 'E2EDataFixtureTests.cs' '"/Account/Signup", bodyText' 11 'generate'
Assert-NoRow 'ignores a Contains asked by a URL receiver' 'E2EDataFixtureTests.cs' 'page.Url.Contains("localhost:9999"' 11
# [regex]::Replace is case-sensitive, so a lowercase receiver needs its own control or the arm silently
# covers page.Url and misses every `url => url.Contains(...)` predicate.
Assert-NoRow 'ignores a lowercase url receiver' 'E2EDataFixtureTests.cs' 'url.Contains("/Account/Lockout"' 11
# `new Uri(x)` is definitionally a URL; the same literal against an ordinary string receiver is not.
Assert-NoRow 'ignores a path asserted against new Uri(...)' 'E2EDataFixtureTests.cs' '"/Admin/Clients", new Uri(' 11
Assert-Row   'flags the same path on a string receiver' 'E2EDataFixtureTests.cs' '"/Admin/Roles", bodyText' 11 'generate'
# The control the *Url arm was missing. `page.Url` is the location under test; `user.AvatarUrl` is a domain
# word with Url glued on, holding a specimen. Anchoring on the suffix alone silenced every `*Url` property
# in the fleet - the arm read them as routes. Two spellings, because one could pass on a coincidence.
Assert-Row   'flags a URL-valued datum on a *Url property' 'E2EDataFixtureTests.cs' 'avatar.jpg' 11 'move-to-sut-constant'
Assert-Row   'flags a URL-valued datum on a second *Url property' 'E2EDataFixtureTests.cs' 'manual.pdf' 11 'move-to-sut-constant'
# `(?<!\w)GetAsync` excludes StringGetAsync but not IDistributedCache.GetAsync. A cache key is a specimen;
# requiring the argument to be route-shaped is what separates it from an endpoint under test.
Assert-Row   'flags a cache key passed to GetAsync' 'E2EDataFixtureTests.cs' 'catalog-page-two' 11 'generate'
# An OData system name is spelled by the specification, so it has no end state to generate toward. The two
# MUST-FIRE rows below are what make this a closed list rather than a "starts with a dollar sign" rule:
# an invented $-prefixed option and an ordinary query key both still report.
# A property name reaching a hand-rolled reader is the same position as GetProperty("x"), which is already
# exempt. `Describe` is the discriminator: same shape, same file, one string parameter, but its body never
# reaches GetProperty, so its literal still reports. Without it the arm could exempt by call shape alone.
Assert-NoRow 'ignores a property name passed to a JSON reader helper' 'E2EDataFixtureTests.cs' 'ReadString(root, "name")' 11
Assert-Row   'flags a literal passed to a helper that reads no property' 'E2EDataFixtureTests.cs' 'name-not-a-property' 11 'generate'
Assert-NoRow 'ignores an OData system query option' 'E2EDataFixtureTests.cs' '"$filter"' 11
Assert-NoRow 'ignores a second OData system query option' 'E2EDataFixtureTests.cs' '"$orderby"' 11
Assert-NoRow 'ignores an OData annotation name' 'E2EDataFixtureTests.cs' '"@odata.count"' 11
Assert-Row   'still flags an invented dollar-prefixed option' 'E2EDataFixtureTests.cs' 'notAnODataOption' 11 'generate'
Assert-Row   'still flags an ordinary query key beside them' 'E2EDataFixtureTests.cs' '"pageSize"' 11 'generate'
# The load-bearing one: seeded data is what rule 11 exists for, and it is in the same file and area as every
# exemption above. If this goes quiet the arms have suppressed the area rather than classified a position.
Assert-Row   'still flags seeded store data in E2E' 'E2EDataFixtureTests.cs' 'Create("LG OLED TV")' 11 'generate'
# A literal that merely ENDS in `@` is not a verbatim opener. `var atSign = "@";` matched the naive `@"`
# regex, found no closing quote after it, and opened a block that swallowed every remaining line of the
# file - rule 11 silenced from there down, with one bogus needs-triage row at the phantom opening. The
# literal on the NEXT line is the control: it must still report, and it cannot if the block opened.
Assert-Row   'still sweeps the line after a literal ending in an at-sign' 'E2EDataFixtureTests.cs' 'Scottsdale' 11 'generate'
# TESTING.md prescribes a prefix match for SELECTING A SET. Flagging these would tell the fleet to stop
# following its own documented pattern; flagging position ON that set is the actual rule.
Assert-NoRow 'ignores a doc-approved prefix set assertion' 'fixture.spec.ts' 'toBeAttached' 10
Assert-NoRow 'ignores a prefix set held in a constant' 'fixture.spec.ts' 'CHURCH_ROWS = ' 10
Assert-NoRow 'ignores a count over a prefix-matched set' 'fixture.spec.ts' 'toHaveCount' 10
Assert-Row   'flags .first() on a TypeScript locator' 'fixture.spec.ts' 'CHURCH_ROWS).first()' 10 'use-id'
Assert-Row   'flags .nth() on a TypeScript locator' 'fixture.spec.ts' 'CHURCH_ROWS).nth(1)' 10 'use-id'

Write-Host "Rule 10 - Playwright's getBy* family, which binds to copy"
# Every pattern above requires a literal locator/querySelector call, so this whole idiom was invisible:
# a Librarian e2e spec held five copy-bound selectors while the catalog reported zero rows for it, twice.
Assert-Row   'flags getByRole with a name' 'fixture.spec.ts' "name: 'Save changes'" 10 'use-id'
Assert-Row   'flags getByText' 'fixture.spec.ts' 'No results found.' 10 'use-id'
Assert-Row   'flags getByLabel' 'fixture.spec.ts' 'Email address' 10 'use-id'
Assert-Row   'flags getByPlaceholder' 'fixture.spec.ts' 'Search titles...' 10 'use-id'
Assert-Row   'flags .first() chained onto a getBy* locator' 'fixture.spec.ts' "name: 'Details' }).first()" 10 'use-id'
# A bare role is document structure, not copy, and a test id is a durable authored hook. Flagging either
# would push suites away from the two selectors that do not track markup or wording.
Assert-NoRow 'ignores a bare getByRole with no name' 'fixture.spec.ts' "getByRole('main')" 10
Assert-NoRow 'ignores getByTestId' 'fixture.spec.ts' 'save-button' 10

Write-Host "Rule 10 - an assertion pins identity, never copy"
Assert-Row   'flags toHaveText on copy' 'fixture.spec.ts' "toHaveText('Clients')" 10 'assert-id-or-url'
Assert-Row   'flags toContainText on copy' 'fixture.spec.ts' "toContainText('Saved')" 10 'assert-id-or-url'
Assert-Row   'flags toHaveTitle on copy' 'fixture.spec.ts' "toHaveTitle('Catalog')" 10 'assert-id-or-url'
Assert-Row   'flags textContent compared with toBe' 'fixture.spec.ts' "textContent()).toBe('Library')" 10 'assert-id-or-url'
Assert-Row   'flags ToHaveTextAsync on copy' 'SelectorFixtureTests.cs' 'ToHaveTextAsync("Clients")' 10 'assert-id-or-url'
Assert-Row   'flags Assert.Equal against TextContentAsync' 'SelectorFixtureTests.cs' 'Assert.Equal("Clients"' 10 'assert-id-or-url'
Assert-Row   'flags a production text constant as copy' 'fixture.spec.ts' 'toContainText(LOAD_ERROR_MESSAGE)' 10 'assert-id-or-url'
Assert-Row   'flags a qualified copy member' 'fixture.spec.ts' 'toHaveText(PageTitles.clients)' 10 'assert-id-or-url'
Assert-Row   'flags text asserted through a local, which a name cannot tell from copy' 'fixture.spec.ts' 'toContainText(seededRunId)' 10 'assert-id-or-url'
Assert-NoRow 'ignores a negative text assertion on a value generated in the file' 'fixture.spec.ts' 'not.toContainText(savedRawgKey)' 10
Assert-NoRow 'ignores a text assertion on a crypto.randomUUID() local' 'fixture.spec.ts' 'toContainText(generatedTitle)' 10
Assert-Row   'flags text asserted through a context member assigned copy' 'fixture.spec.ts' 'toContainText(ctx.copiedBanner)' 10 'assert-id-or-url'
Assert-NoRow 'ignores a negative text assertion on a context member minted by a generator' 'fixture.spec.ts' 'not.toContainText(ctx.mintedAccountId)' 10
Assert-NoRow 'ignores a C# text assertion on a Guid.NewGuid() local' 'SelectorFixtureTests.cs' 'ToHaveTextAsync(generatedClientName)' 10
Assert-NoRow 'ignores toHaveValue' 'fixture.spec.ts' 'toHaveValue(expectedName)' 10
Assert-NoRow 'ignores toHaveAttribute' 'fixture.spec.ts' 'expectedHref' 10
Assert-NoRow 'ignores toHaveURL' 'fixture.spec.ts' 'toHaveURL(expectedUrl)' 10
Assert-NoRow 'ignores toHaveCount' 'fixture.spec.ts' 'expectedRowCount' 10
Assert-NoRow 'ignores ToHaveAttributeAsync' 'SelectorFixtureTests.cs' 'namesOnAnArgument' 10
Assert-Row   'reads a spec under tests\ as a Playwright suite' 'site.spec.ts' "toHaveText('About the site')" 10 'assert-id-or-url'
Assert-NoRow 'leaves a spec colocated with its source in the unit area' 'widget.spec.ts' 'Widget heading' 10
Assert-Row   'flags reading the body inner text' 'fixture.spec.ts' "locator('body').innerText()" 10 'assert-id-or-url'
Assert-Row   'flags reading the page markup' 'fixture.spec.ts' 'await page.content()' 10 'assert-id-or-url'
Assert-Row   'flags textContent of the body' 'fixture.spec.ts' "page.textContent('body')" 10 'assert-id-or-url'
Assert-Row   'flags TextContentAsync of the body' 'SelectorFixtureTests.cs' 'TextContentAsync("body")' 10 'assert-id-or-url'
Assert-Row   'flags ContentAsync of the page' 'SelectorFixtureTests.cs' 'page.ContentAsync()' 10 'assert-id-or-url'
Assert-Row   'flags InnerTextAsync of the body locator' 'SelectorFixtureTests.cs' 'Locator("body").InnerTextAsync()' 10 'assert-id-or-url'
Assert-NoRow 'ignores an id that merely contains body' 'fixture.spec.ts' "locator('#page-body')" 10
Assert-NoRow 'ignores reading an attribute' 'fixture.spec.ts' "getAttribute('data-name')" 10

Write-Host "Rule 10 - the .NET Playwright dialect"
# Loud-on-zero guard: if the C# E2E fixture stops producing rows at all, every per-line assertion below
# would still "pass" as a no-op, which is exactly how this dialect went unnoticed in the first place.
$csharpSelectorRows = @($rows | Where-Object { $_.Path -like '*SelectorFixtureTests.cs' -and [int]$_.Rule -eq 10 })
if ($csharpSelectorRows.Count -eq 0) {
    $failures.Add('MUST FIRE - SelectorFixtureTests.cs produced no rule-10 rows at all; the C# selector dialect is unscanned.')
}
else {
    Write-Host "  PASS  SelectorFixtureTests.cs produced rule-10 rows ($($csharpSelectorRows.Count) rows)"
}
Assert-Row   'flags PageLocatorOptions { HasText = }' 'SelectorFixtureTests.cs' 'HasText = email' 10 'use-id'
# Needles avoid [ and ] - PowerShell -like reads them as a character class, so a bracketed needle is an
# invalid wildcard pattern rather than a literal.
Assert-Row   'flags a tag-only locator' 'SelectorFixtureTests.cs' 'Locator("table")' 10 'use-id'
Assert-Row   'flags .First disambiguation' 'SelectorFixtureTests.cs' 'disambiguated' 10 'use-id'
Assert-Row   'flags a prefix match narrowed by position' 'SelectorFixtureTests.cs' 'prefixThenPosition' 10 'use-id'
Assert-NoRow 'ignores a bare prefix match in C# too' 'SelectorFixtureTests.cs' 'wholeSet' 10
Assert-NoRow 'ignores LINQ First() beside a locator' 'SelectorFixtureTests.cs' 'linqIsNotPosition' 10
# The id'd locator is the ARGUMENT here and the .First belongs to a generated-name pair on the far side of
# the call. Nothing narrows a locator by position, so the line is rule 10 complying, not violating it.
Assert-NoRow 'ignores a .First on an argument beside a locator' 'SelectorFixtureTests.cs' 'namesOnAnArgument' 10
Assert-NoRow 'ignores model-bound input name= in C# too' 'SelectorFixtureTests.cs' 'FillAsync' 10
Assert-NoRow 'ignores a plain id locator' 'SelectorFixtureTests.cs' 'ClickAsync' 10

Write-Host "Rule 11 - generated test data, named locals"
Assert-NoRow 'ignores a well-named local (ownerId)' 'FixtureTests.cs' 'var ownerId = Guid.NewGuid()' 11
Assert-Row   'flags a placeholder local (id)' 'FixtureTests.cs' 'var id = Guid.NewGuid()' 11 'rename-local'
Assert-Row   'flags an inlined generator' 'FixtureTests.cs' 'Lookup(Guid.NewGuid())' 11 'extract-to-named-local'
Assert-NoRow 'ignores xUnit [Trait] plumbing' 'FixtureTests.cs' '[Trait("Category", "Unit")]' 11
# A Reqnroll step pattern binds a .feature line to its method: the feature file fixes its text, so it is
# plumbing like [Trait], not a specimen. The lookalike and the [InlineData] row are the controls: the
# exemption is the exact attribute name, and an attribute string that IS test data still reports.
Assert-NoRow 'ignores a Reqnroll step-binding pattern' 'FixtureTests.cs' '[Given("a member with a confirmed account")]' 11
Assert-Row   'flags a lookalike of a step attribute' 'FixtureTests.cs' '[GivenData("a lookalike step attribute")]' 11 'generate'
Assert-Row   'flags an [InlineData] specimen string' 'FixtureTests.cs' '[InlineData("a theory row specimen")]' 11 'generate'
# `using` opens two constructs. The DIRECTIVE is out of scope; the DECLARATION is an ordinary local, and
# skipping it on the leading keyword made every disposable local in the fleet invisible to rule 11.
# No paired must-not-fire here on purpose: a using DIRECTIVE carries no literal, so "it does not report"
# is true however the skip is written and would be a check that cannot fail. The must-fire below is the
# whole guard.
Assert-Row   'flags a magic number in a using declaration' 'FixtureTests.cs' 'using var expiry = new CancellationTokenSource' 11 'generate'
# Regression: the detector used to charge you for complying. A generator interpolated into a well-named
# local is already rule-11 compliant, but $generatorDeclarationPattern only matched a generator that was
# the ENTIRE right-hand side, so this was reported as an inline violation.
Assert-NoRow 'ignores a generator interpolated into a named local' 'FixtureTests.cs' 'var configuredKey' 11
# Regression: raw-string blocks were invisible to the line-based scan, so SQL placeholders surfaced as
# magic numbers. "Generating" them would destroy the statement.
Assert-NoRow 'ignores numbers inside a raw-string block' 'FixtureTests.cs' 'VALUES ($1, $2, $3, $4)' 11
# The control every exclusion below is measured against: an ordinary literal bound in a test still reports.
Assert-Row   'flags a magic string bound to a local' 'FixtureTests.cs' 'var label = "denomination"' 11 'generate'
# Three literals that are not test data: an exception message nothing asserts on (generating it only makes
# a failure unreadable), a DataTable column label every reader in the fleet ignores in favour of the
# ordinal, and a composite-format template whose `{n}` slots are filled from generated values.
Assert-NoRow 'ignores an exception message literal' 'FixtureTests.cs' 'new InvalidOperationException("the stub' 11
Assert-NoRow 'ignores a DataTable column label' 'FixtureTests.cs' 'table.Columns.Add("Slug"' 11
Assert-NoRow 'ignores an InlineData composite-format template' 'FixtureTests.cs' '"({0}) {1}-{2}"' 11
# An interpolated string whose literal segments are only punctuation is glue between generated values;
# there is no datum left to generate. One with a word in it is still a literal.
Assert-NoRow 'ignores an interpolation whose literal segments are punctuation' 'FixtureTests.cs' '$"{scheme} {token}"' 11
# Concatenation reaches the same glue. `searchKeyword + "%"` and `$"{searchKeyword}%"` differ only in
# syntax, and the carve-out used to require a `$` immediately before the literal - so the author's
# choice of operator decided the verdict. The worded control below is what keeps the widening honest:
# a `+` next to a literal is not a license, the absence of a datum is.
Assert-NoRow 'ignores a concatenated literal that is punctuation only' 'FixtureTests.cs' 'keyword + "%"' 11
Assert-Row   'still flags a concatenated literal carrying a word' 'FixtureTests.cs' 'keyword + " AND "' 11 'generate'
Assert-Row   'flags an interpolation carrying a word' 'FixtureTests.cs' '$"Bearer {token}"' 11 'generate'
# Labels and contract text that are not test data: a SQL parameter name read back through the
# Parameters indexer, SQL text asserted against the captured statement (either dialect), the message
# argument of Assert.True/False, an interpolated exception message, a ToString format specifier.
Assert-NoRow 'ignores a SQL parameter name in a Parameters indexer' 'FixtureTests.cs' 'insert.Parameters' 11
# The same parameter name asked as a membership question. Anchored on BOTH the Parameters receiver and the
# `@` prefix, which the control below pins: an ordinary string argument to any other Contains still fires.
Assert-NoRow 'ignores a SQL parameter name asked through Parameters.Contains' 'FixtureTests.cs' 'insert.Parameters.Contains("@DayOfWeek")' 11
Assert-Row   'still flags a magic string in a non-Parameters Contains' 'FixtureTests.cs' 'insert.Tags.Contains("denomination")' 11 'generate'
Assert-NoRow 'ignores SQL text asserted against the captured statement' 'FixtureTests.cs' 'Assert.Contains("INSERT INTO church"' 11
Assert-NoRow 'ignores SQL Server text and the failure message on one line' 'FixtureTests.cs' 'the writer targets the churches table' 11
# The receiver reaches the statement through an indexer, which is how a captured-command list is read in
# practice (connection.ExecutedCommands[2].CommandText). The lookahead formerly allowed only [\w.], so the
# index broke it and every such assertion reported. Needle carries no bracket: Get-FixtureLine uses -like.
Assert-NoRow 'ignores SQL text asserted through an indexed receiver' 'FixtureTests.cs' 'Assert.Contains("INSERT INTO job_runs"' 11
# The same statement asserted as an INTERPOLATED expected value. The sibling blanking for Assert.True/False
# messages accepts a `$` or `@` prefix on the literal; this one did not,
# so `Assert.Equal("...sql...", cmd.CommandText)` was contract text and `Assert.Equal($"...{x}...sql...",
# cmd.CommandText)` was a magic string. The non-SQL receiver below is the control that keeps the widening
# honest: interpolation is not the license, the *sql/CommandText receiver is.
Assert-NoRow 'ignores interpolated SQL asserted against the statement' 'FixtureTests.cs' "CONTAINS(name, '" 11
Assert-Row   'still flags an interpolation asserted against a non-SQL receiver' 'FixtureTests.cs' 'denomination {keyword} AND parish' 11 'generate'
# The arguments to a random primitive are the generator's DOMAIN, not test data: telling an author to
# "generate" the bounds of a generator is the compliance penalty rule 11 exists to avoid. CODE-STYLE.md
# listed this as a standing false positive rather than teaching it; a parked false positive is a queue
# nobody can dispose of, so it is taught here with a must-fire control beside it.
Assert-NoRow 'ignores the bounds of a random generator' 'FixtureTests.cs' 'var attemptCount = Random.Shared.Next(' 11
# A generator scaled into its domain by arithmetic still has the local naming it: `starRating` IS the
# generated value, and `/ 2.0` puts it on the 0-5 half-step scale the SUT uses. The existing carve-out
# allowed a `.Method()` chain after the generator but not arithmetic, so every such line was charged for
# complying. The control below keeps the real finding firing.
Assert-NoRow 'ignores a generator scaled by arithmetic into a named local' 'FixtureTests.cs' 'var starRating = Random.Shared.Next' 11
# The bounds carve-out, one call further out: a token LENGTH asked of the shared generator class is that
# generator's domain in exactly the way Random.Shared.Next's bounds are. The control directly below is what
# keeps it from blanking the whole line.
Assert-NoRow 'ignores the argument of a shared generator call' 'FixtureTests.cs' 'var denominationWord = Generated.LowercaseToken(6)' 11
Assert-Row   'still flags a magic number beside a shared generator argument' 'FixtureTests.cs' 'Generated.LowercaseToken(6).Length + 250' 11 'generate'
# The receiver is the anchor, so the same call on a per-project generator class earns nothing: that class
# is not rule 11's end state. These two rows keep this a RECEIVER anchor rather than a blanket exemption
# for any method named LowercaseToken.
Assert-Row   'flags the argument of a per-project generator class' 'FixtureTests.cs' 'TestValues.LowercaseToken(7)' 11 'generate'
Assert-Row   'still flags an argument to a receiver that is not a generator' 'FixtureTests.cs' 'Elsewhere.LowercaseToken(9)' 11 'generate'
# A dedicated constants class is the end state for a named constant: the declaration IS the fix rule 11
# asks for, so flagging it charges the author for complying. The file has to earn the name, which the
# control below pins.
Assert-NoRow 'ignores a constant in a dedicated constants class' 'FixtureEventConstants.cs' 'LicenseExpiredEventName' 11
Assert-NoRow 'ignores a numeric constant in a dedicated constants class' 'FixtureEventConstants.cs' 'DroppedEventId' 11
Assert-NoRow 'ignores a static readonly in a dedicated constants class' 'FixtureEventConstants.cs' 'PingInterval' 11
# The exemption has to cover every category rule 11 emits, not just the one the first fixture happened to
# produce: a production-owned identifier declared in a constants class is the same end state as a magic
# string or a magic number declared there.
Assert-NoRow 'ignores a production identifier in a dedicated constants class' 'FixtureEventConstants.cs' 'AuthorizationHeaderName' 11
# A per-project generator class earns its literals no exemption: a hardcoded suffix and an alphabet in it
# report like any other test file's.
Assert-Row   'flags a const in a per-project generator class' 'TestValues.cs' 'const string LegacySuffix' 11 'generate'
Assert-Row   'flags an alphabet in a per-project generator class' 'TestValues.cs' 'const string Punctuation' 11 'generate'
Assert-NoRow 'ignores an assertion failure message' 'FixtureTests.cs' 'the seed must not be empty' 11
Assert-NoRow 'ignores an interpolated exception message' 'FixtureTests.cs' 'was never primed' 11
# A message long enough to wrap sits on its own line, where the single-line blanking cannot see the
# `new XException(` that owns it.
Assert-NoRow 'ignores a wrapped exception message on its own line' 'FixtureTests.cs' 'the enumerator always throws' 11
Assert-NoRow 'ignores a ToString format specifier' 'FixtureTests.cs' 'var sessionKey = Guid.NewGuid().ToString("N")' 11
# A numeric suffix does not turn a boundary into a magic number, and does not hide one either.
Assert-NoRow 'ignores a suffixed zero' 'FixtureTests.cs' 'Assert.Equal(0m, total)' 11
Assert-Row   'flags a suffixed magic number' 'FixtureTests.cs' 'Assert.Equal(5m, total)' 11 'generate'
# A zero fractional part is the double spelling of the same boundary. The controls pin that it hides neither
# a whole magic number nor a fraction that merely starts with a zero.
Assert-NoRow 'ignores a zero-fraction boundary' 'FixtureTests.cs' 'Assert.Equal(1.0, average)' 11
Assert-Row   'flags a zero-fraction magic number' 'FixtureTests.cs' 'Assert.Equal(20.0, average)' 11 'generate'
Assert-Row   'flags a fraction that starts with a zero' 'FixtureTests.cs' 'Assert.Equal(0.05, average)' 11 'generate'
# Rule 11's own text: a test that pins a value asserts the literal against the production constant.
# A literal compared to a RESULT is still a specimen.
Assert-NoRow 'ignores a literal pinned against a production constant' 'FixtureTests.cs' 'Telemetry.Metrics.ConsentGrantedCounterName' 11
Assert-Row   'flags a literal compared to a result' 'FixtureTests.cs' 'Assert.Equal("baptist", church.Denomination)' 11 'generate'
# A generator already has a name when it is assigned to a declared local or IS a generator method's body.
Assert-NoRow 'ignores a generator assigned to an already-named local' 'FixtureTests.cs' 'traceIdentifier = $"trace-' 11
Assert-NoRowWithDisposition 'ignores a generator that is a generator method body' 'FixtureTests.cs' 'NewOwnerId() => Guid.NewGuid()' 11 'extract-to-named-local'
# That same method is still a generator in the wrong PLACE: `Shared.Testing.Generated` is rule 11's end
# state, so a New* method in a test project that draws a value, or only forwards to a Generated generator
# under a local name, reports once per method, and a per-project TestValues.cs is no exception. The
# controls pin that a builder merely consuming generators stays silent and that the body scan stops at the
# method's own closing brace.
Assert-Row   'flags an expression-bodied generator in a test file' 'FixtureTests.cs' 'NewOwnerId() => Guid.NewGuid()' 11 'move-to-generated'
Assert-Row   'flags a block-bodied generator in a test file' 'FixtureTests.cs' 'NewAttemptBudget()' 11 'move-to-generated'
Assert-Row   'flags a RandomNumberGenerator generator in a test file' 'FixtureTests.cs' 'NewSigningKey()' 11 'move-to-generated'
Assert-Row   'flags a pass-through to a Generated generator' 'FixtureTests.cs' 'NewRegionName() => Generated.NewCity()' 11 'move-to-generated'
Assert-NoRowWithDisposition 'ignores a builder that only consumes generators' 'FixtureTests.cs' 'NewChurchFixture(string city)' 11 'move-to-generated'
Assert-NoRowWithDisposition 'stops the body scan at the method end' 'FixtureTests.cs' 'NewDetailedChurchFixture(string city)' 11 'move-to-generated'
Assert-Row   'flags a generator in a per-project generator class' 'TestValues.cs' 'NewZip() => Random.Shared' 11 'move-to-generated'
Assert-Row   'flags a block-bodied generator in a per-project generator class' 'TestValues.cs' 'NewBlankRun()' 11 'move-to-generated'
# A static readonly fixture field is a named home for a generated value (rule 11 discusses the shape).
Assert-NoRow 'ignores a generator bound to a named static field' 'FixtureTests.cs' 'SharedFixtureChatId = Guid.NewGuid()' 11
# An exception's Message and ParamName are text production emits at run time, so production owns them and
# a test that restates them is a second copy (rule 11, ownership by runtime use). Both must report.
Assert-Row   'flags a copied exception message' 'FixtureTests.cs' 'failure.Message);' 11 'generate'
Assert-Row   'flags a copied ParamName' 'FixtureTests.cs' '.ParamName);' 11 'generate'
# The copied-production-text check, per language: an exact restatement and a rendered template must fire;
# a literal production never says, a word under the length floor, the mandated pin shape, a module
# specifier and the same words in another repo's test must not.
Assert-Row   'flags a C# test restating a production literal' 'CopyFixtureTests.cs' 'restatedMessage = ' 11 'borrow-from-production'
Assert-Row   'flags a C# test rendering a production template' 'CopyFixtureTests.cs' 'renderedTemplate = ' 11 'borrow-from-production'
Assert-Row   'flags a C# test restating a production template' 'CopyFixtureTests.cs' 'restatedTemplate = ' 11 'borrow-from-production'
Assert-NoRowWithDisposition 'ignores a C# literal production never says' 'CopyFixtureTests.cs' 'neverSaidByProduction' 11 'borrow-from-production'
Assert-NoRowWithDisposition 'ignores a C# word under the length floor' 'CopyFixtureTests.cs' 'underTheFloor' 11 'borrow-from-production'
Assert-NoRowWithDisposition 'ignores a C# pin against a production constant' 'CopyFixtureTests.cs' 'CopySource.ReadyMessage);' 11 'borrow-from-production'
Assert-NoRowWithDisposition 'ignores the same words in another repo' 'PlainCopyTests.cs' 'anotherReposText' 11 'borrow-from-production'
# The pin exemption keys on a CONSTANT's spelling, so it must be case-sensitive: a lowercase receiver is a
# computed value, and asserting production text against it is the copy, not a pin.
Assert-Row   'flags a C# literal asserted against a lowercase receiver' 'CopyFixtureTests.cs' 'ready.Text);' 11 'borrow-from-production'
Assert-Row   'flags a TypeScript literal asserted against a lowercase receiver' 'copy-source.spec.ts' 'expect(ready)' 11 'borrow-from-production'
# The pin shapes the magic-data sweep already accepts: a literal asserted against a local that reads a
# production member under Act, and a collection of literals asserted against a production member.
Assert-NoRowWithDisposition 'ignores a pin against a production member read under Act' 'CopyFixtureTests.cs' 'Assert.Equal("Widget pipeline ready", readyMessage);' 11 'borrow-from-production'
Assert-NoRowWithDisposition 'ignores a collection pinned against a production member' 'CopyFixtureTests.cs' 'CopySource.AllMessages);' 11 'borrow-from-production'
# A `typeof` operand is a type name the language defines; the same word as an ordinary value still reports.
# xUnit plumbing describes the test, not the subject, whatever production happens to spell the same way.
Assert-NoRowWithDisposition 'ignores a framework attribute that repeats a production word' 'CopyFixtureTests.cs' 'Trait("Category", "Unit")' 11 'borrow-from-production'
Assert-NoRowWithDisposition 'ignores a typeof comparison operand' 'copy-source.spec.ts' 'typeof value ===' 11 'borrow-from-production'
Assert-Row   'flags the same type name as an ordinary value' 'copy-source.spec.ts' "expect(kind).toBe('string')" 11 'borrow-from-production'
Assert-Row   'flags a TypeScript test restating a production literal' 'copy-source.spec.ts' 'toHaveBeenCalledWith(' 11 'borrow-from-production'
Assert-NoRowWithDisposition 'ignores a TypeScript suite title that repeats production text' 'copy-source.spec.ts' "describe('[Worker] pipeline ready'" 11 'borrow-from-production'
Assert-NoRowWithDisposition 'ignores a TypeScript test title that repeats production text' 'copy-source.spec.ts' "test('[Worker] pipeline ready'" 11 'borrow-from-production'
Assert-Row   'flags a TypeScript test rendering a production template' 'copy-source.spec.ts' 'toThrow(' 11 'borrow-from-production'
Assert-NoRowWithDisposition 'ignores a TypeScript pin against a production constant' 'copy-source.spec.ts' 'expect(READY_LOG)' 11 'borrow-from-production'
Assert-NoRowWithDisposition 'ignores a TypeScript literal production never says' 'copy-source.spec.ts' "toBe('Gadget" 11 'borrow-from-production'
Assert-NoRowWithDisposition 'ignores a TypeScript import specifier' 'copy-source.spec.ts' 'import { thing }' 11 'borrow-from-production'
Assert-NoRowWithDisposition 'ignores a TypeScript mocked module specifier' 'copy-source.spec.ts' 'vi.mock(' 11 'borrow-from-production'
Assert-NoRow 'ignores a number pinned against a production constant' 'copy-source.spec.ts' 'expect(RETRY_LIMIT)' 11
Assert-Row   'still flags a number compared to a local' 'copy-source.spec.ts' 'expect(retries)' 11 'generate'
Assert-NoRow 'ignores an escape-only separator' 'copy-source.spec.ts' "parts.join('\n')" 11
Assert-Row   'still flags an escape followed by words' 'copy-source.spec.ts' 'Widget heading' 11 'generate'
Assert-NoRow 'ignores a number inside a template after a URL template on an earlier line' 'copy-source.spec.ts' '%26' 11
Assert-Row   'still flags a plain literal after a URL template' 'copy-source.spec.ts' 'Galveston harbor' 11 'generate'
Assert-NoRow 'ignores a template nested in an interpolation that spans lines' 'copy-source.spec.ts' '<svg xmlns=' 11
Assert-Row   'still flags a plain literal after the nested template closes' 'copy-source.spec.ts' 'Galveston ferry' 11 'generate'
Assert-Row   'flags a Python test restating a production literal' 'test_copy_source.py' 'assert log ==' 11 'borrow-from-production'
Assert-Row   'flags a Python test rendering a production f-string' 'test_copy_source.py' 'assert str(error)' 11 'borrow-from-production'
Assert-NoRowWithDisposition 'ignores a Python pin against a production constant' 'test_copy_source.py' 'assert READY_LOG' 11 'borrow-from-production'
Assert-NoRowWithDisposition 'ignores a Python literal production never says' 'test_copy_source.py' 'assert message' 11 'borrow-from-production'
Assert-NoRowWithDisposition 'ignores a production file that repeats its own text' 'copy_source.py' 'READY_LOG = ' 11 'borrow-from-production'
Assert-NoRowWithDisposition 'ignores Python SQL asserted against a captured statement' 'test_copy_source.py' 'in count_sql' 11 'borrow-from-production'
Assert-NoRowWithDisposition 'ignores Python SQL handed to execute' 'test_copy_source.py' 'cur.execute(' 11 'borrow-from-production'
Assert-NoRowWithDisposition 'ignores a Python pin against a production tuple' 'test_copy_source.py' 'assert TERMINAL_STATUSES ==' 11 'borrow-from-production'
Assert-NoRowWithDisposition 'ignores a Python pin against a private module constant' 'test_copy_source.py' 'copy_source._DATA_STREAM ==' 11 'borrow-from-production'
Assert-NoRowWithDisposition 'ignores a key read from a recorded call' 'test_copy_source.py' 'recorded = call' 11 'borrow-from-production'
Assert-NoRowWithDisposition 'ignores pytest parametrize argnames' 'test_copy_source.py' '@pytest.mark.parametrize(' 11 'borrow-from-production'
Assert-NoRowWithDisposition 'ignores a single-line Python docstring' 'test_copy_source.py' '"""Widgets are counted' 11 'borrow-from-production'
Assert-Row   'still flags a wire key restated in a Python request' 'test_copy_source.py' 'params={"categoryId"' 11 'borrow-from-production'
Assert-Row   'still flags production text handed to an ordinary call' 'test_copy_source.py' 'lookup = fetch(' 11 'borrow-from-production'
Assert-NoRowWithDisposition 'ignores Python SQL wrapped onto the line after execute' 'test_copy_source.py' 'gen.name = %s",' 11 'borrow-from-production'
Assert-NoRowWithDisposition 'ignores a Python tuple pin spread over several lines' 'test_copy_source.py' ') == ("succeeded"' 11 'borrow-from-production'
Assert-NoRowWithDisposition 'ignores a Python dict pin spread over several lines' 'test_copy_source.py' '"DEBUG": "Worker pipeline ready",' 11 'borrow-from-production'
Assert-NoRowWithDisposition 'ignores pytest argnames wrapped onto their own line' 'test_copy_source.py' '    "platform",' 11 'borrow-from-production'
Assert-Row   'still flags production text wrapped into an ordinary call' 'test_copy_source.py' '"Widgets are counted once per owner.",' 11 'borrow-from-production'
Assert-NoRowWithDisposition 'ignores an oracle in a test-owned Python constants module' 'widget_constants.py' '"Worker pipeline ready",' 11 'borrow-from-production'
Assert-Row   'still flags a Python constants module that holds logic' 'lookalike_widget_constants.py' '"Worker pipeline ready",' 11 'borrow-from-production'
Assert-NoRowWithDisposition 'ignores Python SQL concatenated over several lines inside execute' 'test_copy_source.py' "WHERE widget_schema = 'public' " 11 'borrow-from-production'
Assert-Row   'still flags production text concatenated inside an ordinary call' 'test_copy_source.py' '"Owners are summed per widget.",' 11 'borrow-from-production'
Assert-Row   'flags seeded fixture data in a TypeScript test' 'magic-literals.spec.ts' "city: 'Austin'" 11 'generate'
Assert-Row   'flags a magic number in a TypeScript test' 'magic-literals.spec.ts' 'pageSize = 25' 11 'generate'
Assert-Row   'flags a value typed into a field' 'magic-literals.spec.ts' "fill('Phoenix')" 11 'generate'
Assert-NoRow 'ignores a TypeScript module specifier' 'magic-literals.spec.ts' "from '@playwright/test'" 11
Assert-NoRow 'ignores a TypeScript test title' 'magic-literals.spec.ts' "test('seeds a church" 11
Assert-NoRow 'ignores a playwright-bdd step pattern' 'magic-literals.spec.ts' "Given('a member with a confirmed account'" 11
Assert-Row   'flags a lookalike of a step function' 'magic-literals.spec.ts' "setGiven('a lookalike step pattern'" 11 'generate'
Assert-NoRow 'ignores a navigation route' 'magic-literals.spec.ts' "goto('/churches/new')" 11
Assert-NoRow 'ignores a URL assertion' 'magic-literals.spec.ts' "toHaveURL('/churches/created')" 11
Assert-NoRow 'ignores a selector, an expect message and an attribute name' 'magic-literals.spec.ts' 'the count must render' 11
Assert-NoRow 'ignores an array index' 'magic-literals.spec.ts' 'const firstRow' 11
Assert-NoRow 'ignores a typeof operand and an error message' 'magic-literals.spec.ts' 'the seeded value must be text' 11
Assert-NoRow 'ignores the bounds of a random generator' 'magic-literals.spec.ts' 'randomInt(1, 100)' 11
Assert-NoRow 'ignores the title of a parameterized test' 'magic-literals.spec.ts' "renders %s on the card" 11
Assert-NoRow 'ignores an id handed to a ById helper' 'magic-literals.spec.ts' "clickById(page, 'church-save-button')" 11
Assert-Row   'still flags a value handed to a ById helper after the id' 'magic-literals.spec.ts' "'Dallas'" 11 'generate'
Assert-NoRow 'ignores a By.css selector' 'magic-literals.spec.ts' "By.css('tbody tr')" 11
Assert-NoRow 'ignores the global name a test stubs' 'magic-literals.spec.ts' 'fetchMock)' 11
Assert-Row   'still flags the value a stubbed global is given' 'magic-literals.spec.ts' "'not-a-function'" 11 'generate'
Assert-NoRow 'ignores a property key read by index' 'magic-literals.spec.ts' 'const address = graph' 11
Assert-Row   'still flags a literal inside an array literal' 'magic-literals.spec.ts' 'const tags' 11 'generate'
Assert-NoRow 'ignores the URL an HTTP test controller expects' 'magic-literals.spec.ts' "expectOne('/api/churches')" 11
Assert-NoRow 'ignores a selector behind generic type arguments' 'magic-literals.spec.ts' 'const options = fixture' 11
Assert-NoRow 'ignores an id selector handed to any helper' 'magic-literals.spec.ts' 'const box = await boxOf' 11
Assert-Row   'still flags a plain value handed to the same helper' 'magic-literals.spec.ts' 'const labelledBox' 11 'generate'
Assert-Row   'flags an id selector with a descendant, as the C# arm does' 'magic-literals.spec.ts' 'const descendantBox' 11 'generate'
Assert-NoRow 'ignores the property name defineProperty sets' 'magic-literals.spec.ts' 'Object.defineProperty(navigator' 11
Assert-NoRow 'ignores a CSS custom property name' 'magic-literals.spec.ts' 'const shade' 11
Assert-NoRow 'ignores a number inside a backtick template' 'magic-literals.spec.ts' 'CHURCHES_URL' 11
Assert-NoRow 'ignores a quote inside a backtick template' 'magic-literals.spec.ts' 'const renderedPage' 11
Assert-Row   'still flags a number outside a template' 'magic-literals.spec.ts' 'const templatePageSize' 11 'generate'
Assert-NoRow 'ignores the quantifiers of a regular-expression literal' 'magic-literals.spec.ts' 'assert.match(churchId' 11
Assert-Row   'still flags numbers divided outside a regular expression' 'magic-literals.spec.ts' 'const pageRatio' 11 'generate'
Assert-NoRow 'ignores a console status line' 'magic-literals.spec.ts' 'every seeded row reads back' 11
Assert-NoRow 'ignores the closing line of a multi-line import' 'magic-literals.spec.ts' "} from './church-fixtures'" 11
Assert-NoRow 'ignores a property key inside a type argument' 'magic-literals.spec.ts' 'type ScheduleKey' 11
Assert-Row   'still flags the same key used as a value' 'magic-literals.spec.ts' "name: 'Vespers'" 11 'generate'
Assert-NoRow 'ignores an attribute name wrapped onto its own line' 'magic-literals.spec.ts' "    'href'," 11
Assert-Row   'still flags a value wrapped onto its own line' 'magic-literals.spec.ts' "'Norman'," 11 'generate'
Assert-NoRow 'ignores a Playwright page event name' 'magic-literals.spec.ts' "page.on('console'" 11
Assert-NoRow 'ignores a Playwright load state' 'magic-literals.spec.ts' 'networkidle' 11
Assert-NoRow 'ignores a waitUntil load state' 'magic-literals.spec.ts' 'domcontentloaded' 11
Assert-NoRow 'ignores a keyboard key name' 'magic-literals.spec.ts' "press('Enter')" 11
Assert-NoRow 'ignores a browser permission name' 'magic-literals.spec.ts' 'grantPermissions(' 11
Assert-NoRow 'ignores a console message type' 'magic-literals.spec.ts' 'msg.type()' 11
Assert-NoRow 'ignores a CSS property name read through a style helper' 'magic-literals.spec.ts' 'computedStyleOf(page' 11
Assert-NoRow 'ignores a parseInt radix' 'magic-literals.spec.ts' 'parseInt(rawCount, 10)' 11
Assert-Row   'still flags a number beside a radix-less parseInt' 'magic-literals.spec.ts' 'parsedTwice' 11 'generate'
Assert-NoRow 'ignores a sibling file resolved against import.meta.url' 'magic-literals.spec.ts' 'e2e-settings.json' 11
Assert-NoRow 'ignores a parent-relative path resolved against import.meta.url' 'magic-literals.spec.ts' "'../src/pages'" 11
Assert-Row   'still flags a relative path resolved against another base' 'magic-literals.spec.ts' 'fixture-page.json' 11 'generate'
Assert-Row   'still flags a rooted path resolved against import.meta.url' 'magic-literals.spec.ts' 'rooted-page' 11 'generate'
Assert-Row   'still flags a relative path held as a bare string' 'magic-literals.spec.ts' 'bare-settings.json' 11 'generate'
Assert-Row   'routes an HTTP status to a vendor owner' 'magic-literals.spec.ts' 'res.status(404)' 11 'borrow-from-production'
Assert-Row   'routes an asserted HTTP status to a vendor owner' 'magic-literals.spec.ts' 'toBe(401)' 11 'borrow-from-production'
Assert-NoRow 'ignores the reason a test.skip gives' 'magic-literals.spec.ts' 'needs a deployed stack to walk' 11
Assert-Row   'still flags a literal after the skip reason' 'magic-literals.spec.ts' "'Portland'" 11 'generate'
Assert-NoRow 'ignores a synthetic walker action label' 'actions.ts' "name: 'search by keyword'" 11
Assert-NoRow 'ignores a length handed to the shared TypeScript generator' 'shared-generator.spec.ts' 'const shortWord' 11
Assert-Row   'still flags a length handed to a local helper of no import' 'shared-generator.spec.ts' 'const localWord' 11 'generate'
Assert-Row   'still flags a number beside a shared generator call' 'shared-generator.spec.ts' 'const stretched' 11 'generate'
Assert-NoRow 'ignores a text assertion on a shared-generator local' 'shared-generator.spec.ts' 'toHaveText(churchName)' 10
Assert-Row   'still flags a text assertion on a hardcoded local' 'shared-generator.spec.ts' 'toHaveText(typedName)' 10
Assert-NoRow 'ignores the member name handed to vi.spyOn' 'api-members.spec.ts' 'const spy = ' 11
Assert-Row   'still flags a value returned from a spied member' 'api-members.spec.ts' 'const spied = ' 11 'generate'
Assert-NoRow 'ignores a spied member whose object is itself a call' 'api-members.spec.ts' 'const navigate = ' 11
Assert-NoRow 'ignores a DOM tag name compared with tagName' 'api-members.spec.ts' "toBe('BUTTON')" 11
Assert-NoRow 'ignores the string form of an allowed number' 'api-members.spec.ts' "toBe('0')" 11
Assert-NoRow 'ignores an assertion message wrapped onto its own line' 'api-members.spec.ts' "'the wrapped message explains the pin'," 11
Assert-NoRow 'ignores the continuation of a concatenated assertion message' 'api-members.spec.ts' "+ 'across two lines'," 11
Assert-Row   'still flags a literal element of an asserted array' 'api-members.spec.ts' "'Waco'," 11 'generate'
Assert-NoRow 'ignores a status line written to process.stdout' 'api-members.spec.ts' 'process.stdout.write(' 11
Assert-NoRow 'ignores the message of a node:assert assert.ok' 'api-members.spec.ts' 'assert.ok(ready,' 11
Assert-Row   'still flags a value inside an assert.ok condition' 'api-members.spec.ts' "assert.ok(lookup('Killeen'))" 11 'generate'
Assert-NoRow 'ignores the id prefix handed to prefixLocator' 'api-members.spec.ts' 'const rows = prefixLocator' 11
Assert-NoRow 'ignores the id prefix handed to pickFromPrefix' 'api-members.spec.ts' 'const pickedTile' 11
Assert-Row   'still flags a value in the page argument of prefixLocator' 'api-members.spec.ts' 'const cityRows' 11 'generate'
Assert-NoRow 'ignores the percentage form of zero' 'api-members.spec.ts' "toContain('0%')" 11
Assert-Row   'still flags any other percentage' 'api-members.spec.ts' "toContain('7%')" 11 'generate'
Assert-NoRow 'ignores an Angular component selector' 'api-members.spec.ts' "selector: 'app-host-fixture'" 11
Assert-NoRow 'ignores a line inside a multi-line backtick template' 'api-members.spec.ts' '<p>Tyler</p>' 11
Assert-Row   'still flags a literal after the multi-line template closes' 'api-members.spec.ts' "providers: ['Lubbock']" 11 'generate'
Assert-NoRow 'ignores a component input name handed to setInput' 'api-members.spec.ts' "setInput('items'" 11
Assert-NoRow 'ignores a DOM tag name handed to createElement' 'api-members.spec.ts' "createElement('div')" 11
Assert-NoRow 'ignores a test title after a wrapped each table' 'api-members.spec.ts' "])('renders every row" 11
Assert-Row   'still flags a literal inside the wrapped each test' 'api-members.spec.ts' "'Denton'" 11 'generate'
Assert-NoRow 'ignores an assertion message continued after a template line' 'message-shapes.spec.ts' 'content spills past the bottom edge' 11
Assert-NoRow 'ignores an Error message continued after a template line' 'message-shapes.spec.ts' 'A sweep that cannot list' 11
Assert-NoRow 'ignores the message option of an expect.poll on its own line' 'message-shapes.spec.ts' 'the frame neither posted' 11
Assert-NoRow 'ignores the message option of an inline expect.poll' 'message-shapes.spec.ts' 'the inline poll message' 11
Assert-Row   'still flags a message key in a data object' 'message-shapes.spec.ts' "message: 'Galveston'" 11 'generate'
Assert-Row   'still flags a literal continuing a concatenation that is no message' 'message-shapes.spec.ts' "'Amarillo';" 11 'generate'
Assert-Row   'still flags a call named setup in a file with no test alias' 'message-shapes.spec.ts' "setup('Odessa')" 11 'generate'
Assert-NoRow 'ignores a test title under an imported test alias' 'title-alias.setup.ts' "setup('signs in" 11
Assert-Row   'still flags data inside the aliased test' 'title-alias.setup.ts' "fill('Midland')" 11 'generate'
Assert-NoRow 'ignores the source under lint in a RuleTester case' 'rule-tester.spec.ts' 'invalid: [' 11
Assert-NoRow 'ignores a RuleTester case title and code' 'rule-tester.spec.ts' 'valid: [' 11
Assert-NoRow 'ignores RuleTester languageOptions enums' 'rule-tester.spec.ts' 'ecmaVersion: 2022' 11
Assert-NoRow 'ignores the rule name handed to RuleTester.run' 'rule-tester.spec.ts' "ruleTester.run('no-click-navigation'" 11
Assert-Row   'still flags a literal outside the RuleTester cases' 'rule-tester.spec.ts' 'unrelatedCity' 11 'generate'
Assert-NoRow 'ignores RuleTester scaffold concatenated into the cases' 'rule-tester.spec.ts' 'const SCAFFOLD_HEADER' 11
Assert-Row   'still flags a walker weight' 'actions.ts' 'weight: 5' 11 'generate'
Assert-Row   'still flags a walker search term' 'actions.ts' "fill('grace')" 11 'generate'
Assert-NoRow 'ignores every literal in a genuine TypeScript constants file' 'worker-library-constants.ts' 'readyEvent:' 11
Assert-NoRowWithDisposition 'ignores a value declared in a genuine TypeScript constants file' 'worker-library-constants.ts' 'readyEvent:' 11 'borrow-from-production'
Assert-NoRowWithDisposition 'ignores an array value in a genuine TypeScript constants file' 'worker-library-constants.ts' 'levels:' 11 'borrow-from-production'
Assert-NoRowWithDisposition 'ignores a value declared in a genuine C# constants class' 'WidgetLibraryConstants.cs' 'ReadyEvent =' 11 'borrow-from-production'
Assert-Row   'still flags a TypeScript file that only looks like a constants file' 'lookalike-constants.ts' 'readyEvent:' 11 'borrow-from-production'
Assert-NoRowWithDisposition 'ignores the __main__ entry-module name, which the interpreter owns' 'test_copy_source.py' '__name__ ==' 11 'borrow-from-production'
# The URL root is the string form of a boundary sentinel.
Assert-NoRow 'ignores the root path as a boundary' 'FixtureTests.cs' 'Seed().Contains("/")' 11
# A pin against a TEST-owned constant is a pin too (rule 11: the test owns and pins what production never
# reads), so the same exclusion covers it. Recorded as a decision, not an accident.
Assert-NoRow 'ignores a literal pinned against a test-owned constant' 'FixtureTests.cs' 'UnitCollection.Name' 11
# An attribute argument must be a constant expression, so a const an attribute in the same file names is
# compelled by the language, not chosen - there is no generated value or named local that can stand in.
# The control is the const beside it that no attribute names, which must still report; without that this
# would read as "any const in a file containing an attribute is exempt".
Assert-NoRow 'ignores a const an attribute in the same file requires' 'FixtureAttributeHost.cs' 'AttributeCompelledName = "Unit"' 11
Assert-Row   'still flags a const no attribute names' 'FixtureAttributeHost.cs' 'NoAttributeNamesThisOne' 11 'generate'
# The wrapped form of the same pin. A seven-element expected array does not fit on the Assert.Equal line,
# so the literals land on their own line and the production member lands on the next one. There is no code
# fix for this shape - substituting the constants yields Assert.Equal(X, X) - so it is a teaching, and the
# control directly below is what keeps it from covering every collection expression in the fleet.
Assert-NoRow 'ignores a collection expression pinned against a production member' 'FixtureTests.cs' '"$filter", "$select", "$top"' 11
Assert-Row   'still flags a collection expression compared to a local' 'FixtureTests.cs' '"gamma", "delta"' 11 'generate'
# The rule-18 shape of the same pin: the production value is read into a local under // Act, so the
# assertion's partner is lower-case. See Get-PinnedLiteralLines. Three accepted shapes, three controls.
Assert-NoRow 'ignores a literal pinned against a production constant read under Act' 'FixtureTests.cs' '"identity.consent.denied"' 11
Assert-NoRow 'ignores a literal pair pinned against production members read under Act' 'FixtureTests.cs' '"churches.mod", "true"' 11
Assert-NoRow 'ignores an expected-literals local pinned against production members' 'FixtureTests.cs' '"identity.grants.revoked"' 11
Assert-NoRow 'ignores every line of a multi-line expected-literals local' 'FixtureTests.cs' '"identity.exceptions"' 11
Assert-Row   'still flags a literal compared to a computed result read under Act' 'FixtureTests.cs' '"identity.described"' 11 'generate'
# A pinned policy number is the same pin as a pinned name.
Assert-NoRow 'ignores numbers pinned against production members' 'FixtureTests.cs' '371_000' 11
Assert-Row   'still flags a number compared to a computed result' 'FixtureTests.cs' '373, computedBudget' 11 'generate'
Assert-Row   'still flags a literal compared to a local that reuses a name pinned in another test' 'FixtureTests.cs' '"identity.unpinned"' 11 'generate'
Assert-Row   'still flags an expected-literals local compared to a computed collection' 'FixtureTests.cs' '"epsilon", "zeta"' 11 'generate'
# SQL text is a statement in another language, never a specimen to generate. A const earns that only when
# its NAME says Sql and its VALUE opens with a statement keyword; either half alone still reports.
Assert-NoRow 'ignores a wrapped SQL constant named *Sql' 'FixtureTests.cs' '"SELECT status FROM job_runs WHERE run_id = $1"' 11
Assert-NoRow 'ignores an interpolated SQL constant named *Sql' 'FixtureTests.cs' 'SELECT pull_id' 11
Assert-Row   'still flags a *Sql-named constant holding no SQL' 'FixtureTests.cs' '"sigma tau"' 11 'generate'
Assert-Row   'still flags SQL text in a constant not named *Sql' 'FixtureTests.cs' '"SELECT upsilon FROM phi_table"' 11 'generate'
# A local carries the same named statement when interpolation forbids const. Both halves still decide it.
Assert-NoRow 'ignores an interpolated SQL statement in a *Sql-named local' 'FixtureTests.cs' 'FROM tau_table' 11
Assert-Row   'still flags the same SQL text in a local not named *Sql' 'FixtureTests.cs' 'FROM upsilon_table' 11 'generate'
Assert-Row   'still flags a *Sql-named local holding no SQL' 'FixtureTests.cs' 'kappa {columnName} lambda' 11 'generate'
# A nested interpolated string is one literal, not the two fragments a quote-pair scan splits it into.
Assert-NoRow 'ignores the fragments of a nested interpolated string' 'FixtureTests.cs' 'MarkupNames.Cell, $"  {innerValue}  "' 11
Assert-Row   'still flags an ordinary literal beside a nested interpolation' 'FixtureTests.cs' 'iota beside' 11 'generate'
# An exception message stays a label when one of its holes carries a literal of its own.
Assert-NoRow 'ignores an exception message whose hole holds a literal' 'FixtureTests.cs' 'got {Seed().FirstOrDefault() ?? "null"}' 11
Assert-Row   'still flags the same shape outside an exception' 'FixtureTests.cs' 'nu text' 11 'generate'
# An assertion's message is a diagnostic whichever way it reaches the call: inline was already blanked, and a
# const local handed to Assert.True/False in the same test is the same text one statement earlier.
Assert-NoRow 'ignores the first line of a reason constant passed as an assertion message' 'FixtureTests.cs' '"omicron explains why "' 11
Assert-NoRow 'ignores a continuation line of a reason constant' 'FixtureTests.cs' '"the pass must resume."' 11
Assert-Row   'still flags a constant compared as a value' 'FixtureTests.cs' '"pi label"' 11 'generate'
Assert-Row   'still flags a reason-named constant compared as a value' 'FixtureTests.cs' '"rho reason"' 11 'generate'
# A same-file helper whose string parameter reaches a *Sql/*CommandText receiver takes SQL text, directly or
# through another such helper. A helper whose parameter reaches any other receiver still reports.
Assert-NoRow 'ignores SQL text passed to a helper that matches it against ExecutedSql' 'FixtureTests.cs' '"INSERT INTO chi_table"' 11
Assert-NoRow 'ignores SQL text passed to a helper that forwards it to such a helper' 'FixtureTests.cs' '"UPDATE psi_table SET"' 11
Assert-Row   'still flags a literal passed to a helper matching a non-SQL receiver' 'FixtureTests.cs' '"omega label"' 11 'generate'
# The receiver anchor reaches the formatted shapes: an expected value on its own line, a receiver path that
# crosses a call, a null-conditional member access, IndexOf, a receiver on the previous line, and an
# interpolated fragment.
Assert-NoRow 'ignores SQL text wrapped onto its own line before a CommandText receiver' 'FixtureTests.cs' '"pass_name = ''alpha_pass''"' 11
Assert-NoRow 'ignores SQL text against a receiver reached through a call' 'FixtureTests.cs' '"WHERE beta = true"' 11
Assert-NoRow 'ignores SQL text against a null-conditional CommandText' 'FixtureTests.cs' '"FROM gamma_cache"' 11
Assert-NoRow 'ignores SQL text located with IndexOf on a sql receiver' 'FixtureTests.cs' '"FROM delta_entries d"' 11
Assert-NoRow 'ignores SQL text on the line after its ExecutedSql receiver' 'FixtureTests.cs' '"INSERT INTO epsilon_concepts"' 11
Assert-NoRow 'ignores an interpolated SQL fragment against CommandText' 'FixtureTests.cs' '$"INSERT INTO {sql}"' 11
Assert-Row   'still flags a wrapped literal against a non-SQL receiver' 'FixtureTests.cs' '"zeta label"' 11 'generate'
Assert-Row   'still flags IndexOf on a non-SQL receiver' 'FixtureTests.cs' '"eta label"' 11 'generate'
# The suffix list that marks a literal as production-owned is PascalCase because it describes a config key
# name. Matched case-insensitively it also claimed ordinary lower-case words ending in one of them.
Assert-Row   'classifies a lower-case word ending in host as ordinary test data' 'FixtureTests.cs' 'var loopbackHost' 11 'generate'
Assert-Row   'still classifies a PascalCase config key as production-owned' 'FixtureTests.cs' 'var configuredHost' 11 'move-to-sut-constant'
# The four opposite-sign controls for the blanking above: each suppression is anchored to a token, so an
# ordinary literal sharing the line with it must still fire. Without these a broadened pattern would
# silently swallow real rows and the suite would still pass.
# The needle carries no `[`: Get-FixtureLine matches with -like, where a bracket opens a character class.
Assert-Row   'still flags a magic string beside a Parameters indexer' 'FixtureTests.cs' 'Assert.Equal("denomination", insert.Parameters' 11 'generate'
Assert-Row   'still flags SQL-shaped text asserted against a non-SQL receiver' 'FixtureTests.cs' 'commandLog, StringComparison.Ordinal' 11 'generate'
Assert-Row   'still flags a magic string through an indexer on a non-SQL receiver' 'FixtureTests.cs' '.Denomination, StringComparison.Ordinal' 11 'generate'
Assert-Row   'still flags a magic number beside generator bounds' 'FixtureTests.cs' 'Random.Shared.Next(1, 101) + 250' 11 'generate'
Assert-Row   'still flags a hardcoded constant in a per-project generator class' 'TestValues.cs' 'HardcodedDenomination' 11 'generate'
# The opposite-sign control for the constants class: a file that merely ENDS in Constants.cs but carries
# ordinary code beside its literals has not earned the name, and must keep reporting. Without this the
# suppression would be "rename the file to silence the row".
Assert-Row   'still flags a literal in a file that only looks like a constants class' 'NotReallyConstants.cs' 'var label = "baptist"' 11 'generate'
# The opposite-sign control for the arithmetic carve-out: when the generator is an ARGUMENT, the local
# names the result and the generated value is still unnamed and unassertable. That must keep reporting,
# or the carve-out would swallow the shape rule 11 actually exists to catch.
Assert-Row   'still flags a generator passed as an argument' 'FixtureTests.cs' 'Sink.Accept(Guid.NewGuid())' 11 'extract-to-named-local'
Assert-Row   'still flags a magic string inside an assert condition' 'FixtureTests.cs' 'the seed must carry a denomination' 11 'generate'
# The same label shapes as the code is actually formatted: a wrapped Assert message on its own line, a
# target-typed new(...) on an *Exception-returning member, and an *Exception ctor passing base(...).
# A regex handed to Assert.Matches/DoesNotMatch states the PROPERTY being asserted, which is what rule 11
# asks a test about a property to do; no generated value can stand in for `[A-Z]`. First argument only, so
# the pinned specimen on the actual side is the control that stops this becoming "any Assert.Matches line".
Assert-NoRow 'ignores a regex pattern asserted as a property' 'FixtureTests.cs' 'upperCaseProperty);' 11
Assert-NoRow 'ignores a negated regex pattern' 'FixtureTests.cs' 'addressProperty);' 11
Assert-Row   'still flags a pinned specimen on the actual side of a match' 'FixtureTests.cs' 'pinned-actual-specimen' 11 'generate'
Assert-NoRow 'ignores a wrapped assert message on its own line' 'FixtureTests.cs' 'an empty seed is the case under test' 11
# The same diagnostic once it is long enough to need a second line. Both halves must go silent: the first
# line ends mid-expression with no closing paren, the second opens with the `+`.
Assert-NoRow 'ignores the first line of a concatenated wrapped assert message' 'FixtureTests.cs' 'did not overflow the viewport' 11
Assert-NoRow 'ignores the continuation of a concatenated wrapped assert message' 'FixtureTests.cs' 'restored position from a reset one' 11
# The control that makes the arm above discriminate rather than exempt every literal under an Assert.True.
# This literal sits in the CONDITION, wrapped onto its own line inside a nested call, so it is reached with
# the same statement prefix. What tells them apart is the comma count at paren depth one: the message
# argument has passed one, the condition has passed none.
Assert-Row   'still flags a literal wrapped inside an assert condition' 'FixtureTests.cs' 'wrapped-condition-vector' 11 'generate'
Assert-NoRow 'ignores a target-typed exception message' 'FixtureTests.cs' 'NotCalled() => new(' 11
Assert-NoRow 'ignores an exception subclass base message' 'FixtureTests.cs' ': base("fake provider failure")' 11

Write-Host "Rule 8 - a comparer already supplied"
Assert-Row   'reports a bare Distinct for triage' 'FixtureTests.cs' 'Seed().Distinct();' 8 'needs-triage'
Assert-NoRow 'ignores Distinct with a comparer argument' 'FixtureTests.cs' 'EqualityComparer<string>.Default' 8
# A raw block that is a named SQL statement (a const named *Sql opening with a statement keyword) is the
# named constant rule 11 ends at, so it earns no row; any other raw block still reports once.
Assert-NoRow 'ignores a raw-string SQL constant named *Sql' 'FixtureTests.cs' 'const string insertSql' 11
Assert-Row   'reports a raw-string block that is not a named SQL statement, once' 'FixtureTests.cs' 'const string payloadBody' 11 'needs-triage'
Assert-NoRow 'does not sweep inside a raw-string block' 'FixtureTests.cs' '"lambda": 4077' 11
# The same rule for a raw string that fits on one line. It earns its one needs-triage row, and the vendor's
# own ids inside it must NOT also surface as magic numbers - which is what the ordinary `"..."` regex was
# doing, having no way to parse `"""{"id": 201930}"""`. Three assertions because one cannot say it: the row
# exists, the payload's insides do not report, and a literal sharing the line still does.
Assert-Row   'reports a self-contained raw string once, for triage' 'FixtureTests.cs' '"id": 201930, "kind"' 11 'needs-triage'
Assert-NoRowWithDisposition 'does not sweep inside a self-contained raw string' 'FixtureTests.cs' '"id": 201930, "kind"' 11 'generate'
Assert-Row   'still flags a literal beside a self-contained raw string' 'FixtureTests.cs' 'string.Concat(Deserialize' 11 'generate'

Write-Host "Rule 12 - no logic in unit tests"
Assert-Row   'flags if inside a [Theory] body' 'FixtureTests.cs' 'if (exists)' 12 'restructure-split-test'
# The same wrapped-signature blind spot rule 18 hit: a signature whose parameter list wraps was never
# recognised as a test, so the body it guards went unscanned for logic.
Assert-Row   'flags if inside a wrapped-signature test body' 'FixtureTests.cs' 'if (branchFlag)' 12 'restructure-split-test'
Assert-NoRow 'ignores branching inside a named helper' 'FixtureTests.cs' 'if (candidate == key.ToString())' 12
Assert-NoRow 'ignores foreach inside a named helper' 'FixtureTests.cs' 'foreach (var candidate in Seed())' 12
# TypeScript: the it() callback is the body, a named helper may branch, and an it.each table of object
# literals is data that must never open a body early.
Assert-Row   'flags if inside a TypeScript test' 'logic.spec.ts' 'if (picked === 5)' 12 'restructure-split-test'
Assert-Row   'flags else inside a TypeScript test' 'logic.spec.ts' '} else {' 12 'restructure-split-test'
Assert-Row   'flags a loop inside a TypeScript test' 'logic.spec.ts' 'for (const step of [6, 7])' 12 'restructure-use-each'
Assert-Row   'flags switch inside a TypeScript test' 'logic.spec.ts' 'switch (picked)' 12 'restructure-split-test'
Assert-Row   'flags try inside a TypeScript test' 'logic.spec.ts' 'try {' 12 'use-expect-throw'
Assert-Row   'flags catch inside a TypeScript test' 'logic.spec.ts' 'catch (thrown)' 12 'use-expect-throw'
Assert-Row   'flags ?? inside a TypeScript test' 'logic.spec.ts' 'picked ?? 8' 12 'restructure-split-test'
Assert-Row   'flags a ternary inside a TypeScript test' 'logic.spec.ts' "picked > 0 ? 'positive'" 12 'needs-triage'
Assert-Row   'flags branching in the callback after a multi-line it.each row' 'logic.spec.ts' 'if (width === 11)' 12 'restructure-split-test'
Assert-NoRow 'ignores branching inside a TypeScript helper' 'logic.spec.ts' 'if (flag)' 12
Assert-NoRow 'ignores a hook inside test.describe' 'logic.spec.ts' 'if (pickBranch(false) === 9)' 12
Assert-NoRow 'ignores an it.each table row' 'logic.spec.ts' '{ size: 3 }' 12
Assert-NoRow 'ignores .map inside a TypeScript test, as the C# arm ignores LINQ' 'logic.spec.ts' '.map((value) => value)' 12
# Python: a test_ function body, with a helper def and pytest.raises as the controls of opposite sign.
Assert-Row   'flags if inside a Python test' 'test_logic.py' 'if picked == 1:' 12 'restructure-split-test'
Assert-Row   'flags elif inside a Python test' 'test_logic.py' 'elif picked == 3:' 12 'restructure-split-test'
Assert-Row   'flags else inside a Python test' 'test_logic.py' '    else:' 12 'restructure-split-test'
Assert-Row   'flags a loop inside a Python test' 'test_logic.py' 'for step in [1, 2]:' 12 'restructure-use-parametrize'
Assert-Row   'flags a loop whose header spans lines' 'test_logic.py' 'for spread in (' 12 'restructure-use-parametrize'
Assert-Row   'flags while inside a Python test' 'test_logic.py' 'while picked > 5:' 12 'restructure-use-parametrize'
Assert-Row   'flags try inside a Python test' 'test_logic.py' '    try:' 12 'use-pytest-raises'
Assert-Row   'flags except inside a Python test' 'test_logic.py' 'except ValueError:' 12 'use-pytest-raises'
Assert-Row   'flags match inside a Python test' 'test_logic.py' 'match picked:' 12 'restructure-split-test'
Assert-Row   'flags a conditional expression inside a Python test' 'test_logic.py' 'label = 1 if picked else 2' 12 'needs-triage'
Assert-NoRow 'ignores a comprehension, as the C# arm ignores LINQ' 'test_logic.py' '[step * 2 for step in [1, 2]]' 12
Assert-NoRow 'ignores the for clause of a multi-line generator' 'test_logic.py' '        for step in doubled' 12
Assert-NoRow 'ignores the if clause of a multi-line generator' 'test_logic.py' '        if step' 12
Assert-NoRow 'ignores branching inside a Python helper' 'test_logic.py' 'if flag:' 12
Assert-NoRow 'ignores pytest.raises' 'test_logic.py' 'with pytest.raises(ValueError):' 12
Assert-NoRow 'ignores a docstring that mentions if and for' 'test_logic.py' 'A docstring that mentions' 12
# Exempt repos: the same shapes that fire above produce no row under Tools\ or psnpy\.
Assert-NoRow 'exempts the Tools repo' 'ExemptToolsFixture.cs' '=> string.Empty' 1
Assert-NoRow 'exempts the psnpy repo' 'test_exempt_psnpy.py' 'if picked == 1:' 12
$commentCatalogSource = Get-Content -LiteralPath (Join-Path $PSScriptRoot '..\CommentAudit\Get-CommentCatalog.ps1') -Raw
$codeStyleCatalogSource = Get-Content -LiteralPath (Join-Path $PSScriptRoot 'Get-CodeStyleCatalog.ps1') -Raw
$exemptListPattern = '(?m)^\$exemptRepos\s*=\s*@\(([^)]*)\)'
$commentExempt = [regex]::Match($commentCatalogSource, $exemptListPattern).Groups[1].Value
$codeStyleExempt = [regex]::Match($codeStyleCatalogSource, $exemptListPattern).Groups[1].Value
if ($commentExempt -eq '' -or $commentExempt -ne $codeStyleExempt) {
    $script:failures.Add("EXEMPT-LISTS  the two catalogs' `$exemptRepos differ: code-style ($codeStyleExempt), comment ($commentExempt)")
    Write-Host '  FAIL  the two catalogs exempt the same repos' -ForegroundColor Red
}
else {
    Write-Host '  PASS  the two catalogs exempt the same repos'
}
# A theory row drawn from a generator is a condition named by a random value, and xUnit v3 rebuilds its test
# id in every process, which leaves Stryker mutants Pending. Three shapes must fire: generated rows beside a
# static one, a block-bodied source, and a row that reads a static readonly field initialized from a
# generator (the field hides the generator from a line-level look at the source).
Assert-Row   'flags a MemberData source mixing a generated row with a static one' 'FixtureTheoryDataTests.cs' 'MixedRowsSource() => new()' 12 'split-into-facts'
Assert-Row   'flags a block-bodied MemberData source drawing from a generator' 'FixtureTheoryDataTests.cs' 'BlockBodiedGeneratedSource()' 12 'split-into-facts'
Assert-Row   'flags a MemberData source reading a generated static field' 'FixtureTheoryDataTests.cs' 'GeneratedFieldRowsSource =>' 12 'split-into-facts'
# The controls: static rows stay a legitimate theory, and a generator-backed method no [MemberData] names is
# an ordinary helper, not a theory's data.
Assert-NoRow 'ignores a MemberData source of static rows only' 'FixtureTheoryDataTests.cs' 'StaticRowsSource() => new()' 12
Assert-NoRow 'ignores a generator-backed method no MemberData names' 'FixtureTheoryDataTests.cs' 'UnnamedGeneratedHelper() => new()' 12

Write-Host "Rule 13 - no ad-hoc logging"
Assert-Row   'flags Console.WriteLine' 'ProdFixture.cs' 'Console.WriteLine("starting up")' 13 'remove-console-write'

# Rule 13 bans the code narrating. A logger parameter that only satisfies `base(...)` narrates nothing -
# there is no logger in the class to call - so it is COMPLIANT and emits no row at all. It used to emit a
# 'keep-framework-mandated' row, which was a standing-exemption list rebuilt inside the catalog: a row
# nobody could ever act on. CHANGED 2026-09-12 on the operator's instruction; to restore it, re-add the arm
# in Get-CodeStyleCatalog.ps1 and say why a permanently un-actionable row is wanted.
Assert-NoRow 'ignores a logger passed straight to the base constructor' 'FixtureAuthHandler.cs' 'ILoggerFactory logger,' 13
# The exemption is the pass-through SHAPE, not a list of base types. A different base class, and ILogger<T>
# rather than ILoggerFactory, must be judged the same way - the old flag named AuthenticationHandler<T> and
# so reported every other base with the same constructor demand.
Assert-NoRow 'ignores an ILogger<T> passed to a non-handler base constructor' 'FixtureDerivedProcessor.cs' 'ILogger<FixtureDerivedProcessor> logger,' 13
# A primary constructor writes the base TYPE where an ordinary one writes `base`; both are the same shape.
Assert-NoRow 'ignores a logger passed through a primary constructor base initializer' 'FixturePrimaryCtorHandler.cs' 'ILoggerFactory logger,' 13
# THE control, and the case the old file-level flag silently exempted: same base, same parameter, but this
# class assigns it to a field. It now holds a logger of its own, which is the thing rule 13 bans, so it must
# still report. If this goes quiet the exemption has become "any logger near a base initializer".
Assert-Row   'still flags a base-constructor logger the class keeps' 'FixtureKeptLoggerHandler.cs' 'ILoggerFactory logger,' 13 'remove-ad-hoc-logging'
# Removing the logging service is the opposite of narrating with it, and a DI registration is not a log call.
Assert-NoRow 'ignores RemoveAll<ILoggerFactory>' 'FixtureAuthHandler.cs' 'RemoveAll<ILoggerFactory>' 13
# The control for the ordinary arm: an ILoggerFactory dependency that reaches no base constructor at all is
# an ad-hoc logging dependency and must still report.
Assert-Row   'still flags an ordinary ILoggerFactory dependency' 'ProdFixture.cs' 'OrdinaryLoggerFactoryDependency' 13 'remove-ad-hoc-logging'

Write-Host "Rule 14 - no authored CSS"
Assert-NoRow 'ignores a top-level @import' 'fixture-styles.css' "@import 'tailwindcss'" 14
Assert-NoRow 'ignores a daisyUI theme plugin token' 'fixture-styles.css' '--color-plugin-token' 14
Assert-NoRow 'ignores an @theme token' 'fixture-styles.css' '--color-canvas: oklch(0.18' 14
Assert-NoRow 'ignores an @font-face descriptor' 'fixture-styles.css' "font-family: 'Fixture Sans'" 14
Assert-NoRow 'ignores a colour scheme re-binding an @theme name' 'fixture-styles.css' '--color-canvas: oklch(0.97' 14
Assert-Row   'flags a colour scheme minting a name @theme never defines' 'fixture-styles.css' '--color-minted-here' 14 'move-to-theme'
Assert-Row   'flags a colour scheme aliasing an @theme name through var()' 'fixture-styles.css' '--color-rebound: var(' 14 'move-to-theme'
Assert-Row   'flags a custom property on :root outside a colour scheme' 'fixture-styles.css' '--space-fixture' 14 'move-to-theme'
Assert-NoRow 'ignores an @layer base type selector' 'fixture-styles.css' 'var(--font-heading-fixture)' 14
Assert-NoRow 'ignores an @layer base type selector with an attribute and a pseudo-class' 'fixture-styles.css' 'border-color: var(--color-canvas)' 14
Assert-Row   'flags an @layer base selector naming a class inside :not()' 'fixture-styles.css' 'var(--color-link-fixture)' 14 'use-utilities'
Assert-Row   'flags an @layer base universal selector' 'fixture-styles.css' 'box-sizing: border-box' 14 'use-utilities'
Assert-Row   'flags an @layer base descendant combinator' 'fixture-styles.css' 'var(--color-nav-fixture)' 14 'use-utilities'
Assert-Row   'flags an @layer base class selector' 'fixture-styles.css' 'margin: 0;' 14 'use-utilities'
Assert-Row   'flags an @layer components primitive' 'fixture-styles.css' 'display: inline-flex' 14 'use-utilities'
Assert-Row   'flags an authored @utility' 'fixture-styles.css' 'z-index: 10' 14 'use-utilities'
Assert-Row   'flags authored @keyframes' 'fixture-styles.css' 'rotate(360deg)' 14 'use-utilities'
Assert-NoRow 'ignores a closing brace inside a quoted value' 'fixture-styles.css' 'var(--quoted-fixture-color)' 14
Assert-NoRow 'ignores a declaration on the first line of a comment' 'fixture-styles.css' '/* a { color: red; }' 14
Assert-NoRow 'ignores a rule inside a comment' 'fixture-styles.css' 'commented-out-fixture' 14
Assert-Row   'flags the declaration after a comment, on its own line' 'fixture-styles.css' 'opacity: 0.5' 14 'use-utilities'
Assert-Row   'flags a component stylesheet declaration' 'fixture.component.css' 'padding: 1rem' 14 'use-utilities'
Assert-NoRow 'ignores a vendored wwwroot\lib stylesheet' 'vendor-fixture.css' '.vendor' 14
Assert-NoRow 'ignores a generated Stryker report' 'stryker-fixture-report.html' 'stryker report fixture' 14
Assert-NoRow 'ignores a generated Cucumber report' 'cucumber-fixture-report.html' 'cucumber report fixture' 14
Assert-Row   'a directory merely starting with cucumber-report stays reported' 'cucumber-lookalike.html' 'cucumber lookalike fixture' 14 ''
Assert-NoRow 'ignores a generated playwright-bdd spec' 'bddgen-fixture.spec.ts' "fill('Tulsa')" 11
Assert-Row   'a features-gen directory without the dot stays reported' 'bddgen-lookalike.spec.ts' "fill('Tempe')" 11 'generate'
Assert-Row   'flags a static style attribute in HTML' 'fixture-page.html' 'max-height: 320px' 14 'use-utilities'
Assert-NoRow 'ignores an Angular [style.x] binding' 'fixture-page.html' '[style.width.px]' 14
Assert-NoRow 'ignores an Angular [attr.style] binding' 'fixture-page.html' '[attr.style]' 14
Assert-NoRow 'ignores an empty style attribute' 'fixture-page.html' 'empty style fixture' 14
Assert-NoRow 'ignores an attribute whose name only ends in style' 'fixture-page.html' 'data attribute fixture' 14
Assert-Row   'flags a declaration inside a <style> block' 'fixture-page.html' 'color: red;' 14 'use-utilities'
Assert-NoRow 'ignores an Astro style expression' 'FixtureCard.astro' 'astro dynamic fixture' 14
Assert-Row   'flags a static Astro style attribute' 'FixtureCard.astro' 'astro static fixture' 14 'use-utilities'
Assert-Row   'flags a static Razor style attribute' 'FixtureView.cshtml' 'razor fixture' 14 'use-utilities'
Assert-Row   'flags an Angular component inline styles array' 'inline-styles.component.ts' "styles: ['.x" 14 'use-utilities'
Assert-NoRow 'ignores an Angular styleUrl' 'inline-styles.component.ts' 'styleUrl:' 14

Write-Host "Rule 17 - no manual timeouts in tests"
# Loud-on-zero guard, for the same reason as the dispatch guards above: a dispatch arm that never runs turns
# every MUST-NOT-FIRE below into a pass.
$timeoutRows = @($rows | Where-Object { [int]$_.Rule -eq 17 })
if ($timeoutRows.Count -eq 0) {
    $failures.Add('DISPATCH  rule 17 produced ZERO rows - Scan-TimeoutLine is not being called, so every MUST-NOT-FIRE below is vacuous.')
    Write-Host '  FAIL  rule 17 produced at least one row' -ForegroundColor Red
}
# A named constant is the same timeout. If the constant case goes quiet, every suite clears the rule with one
# `const`, which is the exact shape the Librarian and Inventory walkers already use.
Assert-Row   'flags a timeout option on a TypeScript assertion' 'fixture.spec.ts' 'timeout: 10_000' 17 'remove-timeout'
Assert-Row   'flags a TypeScript timeout held in a named constant' 'fixture.spec.ts' 'timeout: RENDER_TIMEOUT_MS' 17 'remove-timeout'
Assert-Row   'flags page.setDefaultTimeout' 'fixture.spec.ts' 'setDefaultTimeout(60_000)' 17 'remove-timeout'
Assert-Row   'flags test.setTimeout' 'fixture.spec.ts' 'test.setTimeout(120_000)' 17 'remove-timeout'
Assert-Row   'flags test.slow()' 'fixture.spec.ts' 'test.slow()' 17 'remove-timeout'
Assert-Row   'flags waitForTimeout' 'fixture.spec.ts' 'waitForTimeout(500)' 17 'await-the-condition'
Assert-Row   'flags a Promise wrapped around setTimeout' 'fixture.spec.ts' 'setTimeout(resolve, 250)' 17 'await-the-condition'
Assert-Row   'flags a TypeScript helper that accepts a timeout' 'fixture.spec.ts' 'renderTimeoutMs: number' 17 'remove-timeout'
Assert-NoRow 'ignores the word timeout inside copy' 'fixture.spec.ts' 'timeout: none' 17
# A mock scheduling simulated latency is behaviour a spec observes, not a wait; both of its lines stay silent.
Assert-NoRow 'ignores a setTimeout that schedules a callback' 'fixture.spec.ts' 'setTimeout(() => {' 17
Assert-NoRow 'ignores the delay line of that schedule' 'fixture.spec.ts' '}, 300);' 17

Assert-Row   'flags a Playwright options Timeout' 'SelectorFixtureTests.cs' 'PageAssertionsToHaveURLOptions { Timeout = 60_000 }' 17 'remove-timeout'
Assert-Row   'flags an options Timeout held in a named constant' 'SelectorFixtureTests.cs' 'Timeout = CeremonyTimeoutMs' 17 'remove-timeout'
Assert-Row   'flags an options Timeout alone on a wrapped line' 'SelectorFixtureTests.cs' 'Timeout = 10_000' 17 'remove-timeout'
Assert-Row   'flags SetDefaultTimeout' 'SelectorFixtureTests.cs' 'SetDefaultTimeout(60_000)' 17 'remove-timeout'
Assert-Row   'flags WaitForTimeoutAsync' 'SelectorFixtureTests.cs' 'WaitForTimeoutAsync(500)' 17 'await-the-condition'
Assert-Row   'flags a Task.Delay with no TimeProvider' 'SelectorFixtureTests.cs' 'Task.Delay(100)' 17 'await-the-condition'
Assert-Row   'flags a C# helper that accepts a timeout' 'SelectorFixtureTests.cs' 'TimeSpan? timeout = null' 17 'remove-timeout'
Assert-NoRow 'ignores the word Timeout inside a string' 'SelectorFixtureTests.cs' 'var timeoutLabel' 17
Assert-NoRow 'ignores catching TimeoutException' 'SelectorFixtureTests.cs' 'exception is TimeoutException' 17
Assert-NoRow 'ignores the declaration of a timeout constant, whose uses report' 'SelectorFixtureTests.cs' 'private const float CeremonyTimeoutMs' 17

Assert-Row   'flags [Fact(Timeout = ...)]' 'FixtureTests.cs' 'Timeout = 5000' 17 'remove-timeout'
Assert-Row   'flags a CancellationTokenSource built with a delay' 'FixtureTests.cs' 'using var expiry = new CancellationTokenSource' 17 'remove-timeout'
Assert-Row   'flags CancelAfter' 'FixtureTests.cs' 'deadlineSource.CancelAfter' 17 'remove-timeout'
Assert-Row   'flags WaitAsync(TimeSpan)' 'FixtureTests.cs' 'WaitAsync(TimeSpan.FromSeconds(5))' 17 'remove-timeout'
Assert-NoRow 'ignores a CancellationTokenSource with no delay' 'FixtureTests.cs' 'using var deadlineSource' 17
Assert-NoRow 'ignores WaitAsync on the test cancellation token' 'FixtureTests.cs' 'await work.WaitAsync(TestContext' 17
Assert-NoRow 'ignores a Task.Delay driven by a fake clock' 'FixtureTests.cs' 'fakeTimeProvider' 17

# The Area controls. Each fixture reaches rule 17 only through Get-Area's file-name or directory
# classification, so a regression there reads as a missing row here rather than as a silent zero.
Assert-Row   'flags a Vitest spec beside its source' 'session.spec.ts' 'vi.setConfig' 17 'remove-timeout'
Assert-Row   'flags a timeout in the synthetic-walker engine' 'walker.ts' 'PASSKEY_SUBMIT_TIMEOUT_MS' 17 'remove-timeout'
Assert-NoRow 'ignores the compiled dist-test copy of the walker engine' 'compiled-walker.d.ts' 'waitForAngularHydration' 17
Assert-NoRow 'ignores the operator enrollment script beside the walker engine' 'enroll-passkey.ts' 'ENROLLMENT_TIMEOUT_MS' 17
Assert-NoRow 'ignores a production HttpClient.Timeout' 'ProdFixture.cs' 'httpClient.Timeout =' 17
Assert-Row   'flags a timeout in a .Tests.Load project, test code by its directory' 'LoadAreaFixture.cs' 'loadDeadline.CancelAfter' 17 'remove-timeout'
Assert-NoRow 'ignores production socket timeouts' 'prod.ts' 'redisSocketOptions' 17
Assert-Row   'flags expect.timeout in a Playwright config' 'playwright.config.ts' 'expect: { timeout' 17 'remove-timeout'
Assert-Row   'flags actionTimeout in a Playwright config' 'playwright.config.ts' 'actionTimeout: 30_000' 17 'remove-timeout'
Assert-Row   'flags navigationTimeout in a Playwright config' 'playwright.config.ts' 'navigationTimeout: 60_000' 17 'remove-timeout'
Assert-Row   'flags testTimeout in a Vitest config' 'vitest.config.ts' 'testTimeout: 15000' 17 'remove-timeout'
Assert-Row   'flags testTimeout in an ESM Vitest config (.mts)' 'vitest.config.mts' 'testTimeout: 25_000' 17 'remove-timeout'
Assert-Row   'flags a Vitest spec beside its source written as .mts' 'session.spec.mts' 'hookTimeout: 30_000' 17 'remove-timeout'

Write-Host "Rule 18 - AAA markers in unit tests"
# The compliant control is the discriminating half: without it the detector could report every test
# method and still pass the must-fire rows below.
Assert-NoRow 'ignores a test carrying all three AAA markers' 'FixtureTests.cs' 'AaaMarkersPresent_SeparatesTheThreePhases' 18
Assert-Row   'flags a test whose body has no Act marker' 'FixtureTests.cs' 'AaaMarkersAbsent_FusesActIntoTheAssertion' 18 'add-aaa-markers'
Assert-Row   'flags a combined Act & Assert marker' 'FixtureTests.cs' 'AaaMarkersCombined_LabelsActAndAssertTogether' 18 'split-act-from-assert'
Assert-Row   'flags an expression-bodied test, which has no body to divide' 'FixtureTests.cs' 'ExpressionBodiedTest' 18 'add-aaa-markers'
# A wrapped parameter list leaves no closing paren on the signature line. Matching a call shape per line
# instead reported `aaa-missing-in-Setup` against a Moq call inside a real Identity test while never
# seeing the test itself, so both halves are pinned: the chained lambda must not be read as a signature,
# and the wrapped test must still be judged on its own markers.
Assert-NoRow 'does not mistake a chained lambda for a test signature' 'FixtureTests.cs' 'v => Echo(v))' 18
Assert-NoRow 'ignores a wrapped signature whose body carries markers' 'FixtureTests.cs' 'AaaWrappedSignature_WithMarkers_IsCompliant' 18
Assert-Row   'flags a wrapped signature whose body has no markers' 'FixtureTests.cs' 'AaaWrappedSignature_WithoutMarkers_IsReported' 18 'add-aaa-markers'
Assert-Row   'flags an Arrange marker with nothing under it' 'FixtureTests.cs' 'AaaEmptyArrange_LabelsNothingAtAll' 18 'remove-empty-arrange'
Assert-NoRowWithDisposition 'ignores an Arrange block that actually arranges' 'FixtureTests.cs' 'AaaMarkersPresent_SeparatesTheThreePhases' 18 'remove-empty-arrange'
# The two shapes the original three checks CANNOT see: a body with three `// Assert` markers has an Act,
# is not combined, and has a non-empty Arrange, so it satisfied every one of them. Rule 18 read zero
# across the workspace while three real violations stood, one of them a red build.
Assert-Row   'flags a duplicated Assert marker' 'FixtureTests.cs' 'AaaDuplicateAssert_StampsOneMarkerPerAssertionGroup' 18 'remove-duplicate-marker'
Assert-Row   'flags Assert placed before Act' 'FixtureTests.cs' 'AaaOutOfOrder_AssertsAPreconditionBeforeTheAct' 18 'reorder-aaa-markers'
# The load-bearing negatives. A property-based test marks inside the sample lambda while the generator
# sits under an outer `// Arrange` (CODE-STYLE.md rule 18), so a count that does not reset at the lambda
# reports every CsCheck test in the fleet. Sibling lambdas at the SAME depth must not merge either.
Assert-NoRowWithDisposition 'ignores an outer and inner Arrange in a property-based test' 'FixtureTests.cs' 'AaaPropertyBased_MarksInsideTheSampleLambda' 18 'remove-duplicate-marker'
Assert-NoRowWithDisposition 'ignores two sibling lambdas each carrying an Assert' 'FixtureTests.cs' 'AaaSiblingLambdas_EachCarryTheirOwnAssert' 18 'remove-duplicate-marker'
Assert-NoRowWithDisposition 'does not call the property-based shape out of order' 'FixtureTests.cs' 'AaaPropertyBased_MarksInsideTheSampleLambda' 18 'reorder-aaa-markers'
Assert-NoRow 'ignores the whole-test budget in a config' 'playwright.config.ts' 'timeout: 10' 17

Assert-NoRow 'ignores the server-start budget in a config' 'playwright.config.ts' '    timeout: 60_000' 17

Write-Host ""
Write-Host "-RepoPath - a checkout under another folder name is scanned and exempted as the repo it declares"
$sample = $rows | Where-Object { $_.Repo -ceq 'FixtureApp' -and $_.Category -cne 'copied-production-text' } | Select-Object -First 1
$renamedParent = New-ClaudeTempPath ("catalog-repopath-" + [guid]::NewGuid().ToString('N'))
$renamed = Join-Path $renamedParent 'hooks-folder-branch'
try {
    if (-not $sample) {
        $failures.Add('REPOPATH  the fixture produced no FixtureApp row to copy, so the -RepoPath rows would assert nothing')
        Write-Host '  FAIL  a FixtureApp row exists to copy' -ForegroundColor Red
    }
    else {
        $inside = $sample.Path.Substring($sample.Path.IndexOf('\') + 1)
        New-Item -ItemType Directory -Force -Path (Split-Path -Parent (Join-Path $renamed $inside)) | Out-Null
        Copy-Item -LiteralPath (Join-Path $fixtureRoot $sample.Path) -Destination (Join-Path $renamed $inside)
        $repoPathCases = @(
            @{ Name = 'must-block: a checkout declared Tools is exempt whatever its folder is called'; Repo = 'Tools'; Expect = { param($found) $found.Count -eq 0 } }
            @{ Name = 'control: the same checkout declared as a fleet repo is scanned, its rows under that name'; Repo = 'FixtureApp'; Expect = { param($found) $found.Count -gt 0 -and @($found | Where-Object { $_.Repo -cne 'FixtureApp' }).Count -eq 0 } }
            @{ Name = 'control: with no -Repo the folder name is the repo'; Repo = $null; Expect = { param($found) $found.Count -gt 0 -and @($found | Where-Object { $_.Repo -cne 'hooks-folder-branch' }).Count -eq 0 } }
        )
        foreach ($case in $repoPathCases) {
            $caseCsv = Join-Path $renamedParent ("{0}.csv" -f [guid]::NewGuid().ToString('N'))
            if ($case.Repo) { & $scriptUnderTest -RepoPath $renamed -Repo $case.Repo -OutputPath $caseCsv | Out-Null }
            else { & $scriptUnderTest -RepoPath $renamed -OutputPath $caseCsv | Out-Null }
            $found = @(if (Test-Path -LiteralPath $caseCsv) { Import-Csv -LiteralPath $caseCsv })
            if (& $case.Expect $found) { Write-Host "  PASS  $($case.Name)" }
            else {
                $failures.Add("REPOPATH  $($case.Name): $($found.Count) row(s), repos $(@($found.Repo | Sort-Object -Unique) -join ', ')")
                Write-Host "  FAIL  $($case.Name)" -ForegroundColor Red
            }
        }
    }
}
finally {
    for ($attempt = 1; Test-Path -LiteralPath $renamedParent; $attempt++) {
        try { Remove-Item -LiteralPath $renamedParent -Recurse -Force; break }
        catch [IO.IOException] {
            if ($attempt -ge 5) { throw }
            Start-Sleep -Milliseconds 200
        }
    }
}

Write-Host ""
Write-Host "CODE-STYLE.md - the caveats that explain these detectors must survive in the doc"
# The detectors above are only half the guard. Each one has a caveat in CODE-STYLE.md explaining why it
# fires where it does, and a compression pass that trims the prose disarms the human half while every
# assertion here still passes. These phrases were previously a standalone AGENTS/CODE-STYLE.assertions.txt
# run by hand through the backlog-audit skill's validate_doc.py - which meant, in practice, run by nothing.
# They live here because whoever edits a detector is exactly who might trim its explanation.
#
# TWO RULES FOR EDITING THIS LIST, both learned the expensive way:
#   * Keep every phrase short enough to sit on ONE source line. CODE-STYLE.md is hard-wrapped at ~100
#     chars and a phrase spanning a line break never matches, which reads as a deleted rule when nothing
#     was deleted. That cost two false alarms in the sibling fixture.
#   * Never assert on a section number. A number can be legitimately re-used by new content, and the
#     check then fires on correct work. Assert on the wording that carries the rule.
# CODE-STYLE.md sits in this repository's AGENTS folder. TESTING.md is the crgolden workspace's, which
# gate.ps1 hands down as CRGOLDEN_WORKSPACE; only a run that chose -Standalone (TOOLS_GATE_STANDALONE) may go
# without it, and it says so instead of passing quietly.
$fleetRoot = $env:CRGOLDEN_WORKSPACE
$codeStyleDoc = Join-Path (Split-Path -Parent $PSScriptRoot) 'AGENTS\CODE-STYLE.md'

$docPhrases = @(
    # The rule-11 false-positive classes. A pass that blindly re-queues one of these vandalises compliant
    # code, so the doc must keep describing them and this list is what guarantees that.
    #
    # CHANGED 2026-09-12, and the reason matters because this list constrains whoever edits it. It used to
    # also pin 'The reachable per-file end state is' — the sentence declaring those classes permanently
    # exempt and zero rows unreachable. The operator's standing directive is that every catalog row ends as
    # a code fix or a HARNESS-PROVEN DETECTOR FIX, and that a disposition written into a doc is a claim to
    # remove rather than an exemption to honour. That pin made this harness enforce the opposite, so the
    # doc could not be corrected without the suite failing. The guard is kept, its polarity is not: the doc
    # must still NAME each class, and must now also say that each is work owed. A parked false positive is
    # a queue nobody can dispose of — `Get-CommentCatalog.ps1` reached the same conclusion for toolchain
    # directives and taught them instead. To restore the old behaviour, re-add the phrase and say why.
    # RE-POINTED after crgolden-33's rewrite. This used to pin 'the query text is the thing under test'.
    # The caveat is intact; the wording moved and improved. The doc now names the discriminator rather
    # than the justification, so the phrase pinned is the discriminator.
    'The receiver is the anchor'
    'flagged by its own literal'
    'a policy threshold the SUT owns'
    'not a standing exemption'
    # ADDED 2026-09-13 on the operator's correction. The `*Constants.cs` exemption was granted for one
    # case - constants mirrored out of Duende's own licensing code, which this fleet does not own - and a
    # sweep then generalised it into "move any constant and the row clears". These two phrases carry the
    # ownership test that stops that reading, so they are pinned rather than left to survive a trim.
    # Lower-cased by crgolden-33's rewrite, which strips shouty capitals fleet-wide. The caveat is
    # unchanged; only the capitals moved.
    'The governing question is ownership'
    'Relocating a value we own into a'
    # Detector caveats that cost a round-trip each when forgotten. Only the ones a person still has to
    # hold in their head belong here; a caveat the detector itself now enforces is asserted by the
    # fixture cases above, and keeping it in prose as well is the duplicated-claim problem this list has.
    # "blind to a call wrapped across lines" left when the balance scan learned to follow a call past the
    # end of the line; the empty-list and dispatch guards cover the two that described this harness.
    'A line can appear under two rules'
    # Rule 18's scope reset. The two newest checks count markers per scope, and the property-based negatives
    # in the fixture look like arbitrary exemptions without this sentence: CODE-STYLE.md rule 18 permits a
    # marker inside a CsCheck sample lambda under another one outside it.
    'a count that does not reset at the lambda'
    'cannot see the receiver''s type'
    # Rule 14's blind spots: styling assigned from script is not a declaration the parser can see.
    'It cannot see styling assigned from script'
    # Rules that no detector can enforce, so only this list holds them.
    'fails open under a column type change'
    'grep-driven'
    # The standing policy. If this inverts, someone has decided the regex catalog is primary.
    'Analyzers first; the catalog is the fallback'
    # ADDED 2026-09-14 with rule 17. The two sentences a trim would most likely soften: without the first a
    # named constant reads as a fix, and without the second a raised default reads as outside the rule.
    'A named constant is the same timeout'
    'Raising a default is a timeout on every call'
    # The copied-production-text check. The first is the rule it enforces; the second is how a rendered
    # message is caught, which a trim to "exact matches" would silently narrow.
    'Production text is borrowed, never restated'
    'renders one of its templates'
    # The TypeScript literal sweep: which positions name something outside the test, and the status-code route.
    'literal that opens its line is judged against the call that opened on the line before'
    '`code` is the source under lint'
    'a test''s own SQL handed to the driver'
    'RFC 9110 owns it'
)

# A caveat belongs to whichever doc owns its subject, and two of these moved to TESTING.md when the text
# was reorganised. Asserting them against CODE-STYLE.md reported the rule as deleted while it was intact
# a file away - a wrong-file assertion, not missing prose, and it stayed red for reasons nobody could act
# on. Two further phrases were dropped rather than re-pointed: nothing in any doc, repo or Syncthing
# version records what "an assumption wearing the clothes of a fact" or "yields the four-character
# string" protected, and an assertion whose subject nobody can name guards nothing.
$testingDoc = if ($fleetRoot) { python (Join-Path (Split-Path -Parent $PSScriptRoot) 'Hooks\session_home.py') doc 'AGENTS/TESTING.md' }

$testingDocPhrases = @(
    # Measuring rendered TEXT width, and the zero-measurement trap that passes every "does it fit?" check.
    'never `scrollWidth`'
    'satisfies every upper bound'
)

# Loud on zero, same reason as the dispatch guards above: an unreadable doc or an emptied list would make
# every phrase check below vacuous, and the section would report clean while guarding nothing.
function Test-DocPhrases([string]$docName, [string]$docPath, [string[]]$phrases) {
    if (-not (Test-Path -LiteralPath $docPath)) {
        $failures.Add("DOC  $docName not found at '$docPath' - every phrase assertion for it is vacuous.")
        Write-Host "  FAIL  $docName is readable" -ForegroundColor Red
        return
    }
    if ($phrases.Count -eq 0) {
        $failures.Add("DOC  the $docName phrase list is empty - this section is asserting nothing.")
        Write-Host "  FAIL  the $docName phrase list is non-empty" -ForegroundColor Red
        return
    }

    $docText = Get-Content -LiteralPath $docPath -Raw
    $missing = @($phrases | Where-Object { -not $docText.Contains($_) })
    foreach ($phrase in $missing) {
        $failures.Add("DOC  $docName no longer contains: $phrase")
        Write-Host "  FAIL  $phrase" -ForegroundColor Red
    }
    if ($missing.Count -eq 0) {
        Write-Host "  PASS  all $($phrases.Count) rule phrases still present in $docName"
    }
    else {
        Write-Host "  ---   $($phrases.Count - $missing.Count) of $($phrases.Count) rule phrases still present in $docName" -ForegroundColor Red
    }
}

Test-DocPhrases 'CODE-STYLE.md' $codeStyleDoc $docPhrases
if ($fleetRoot) {
    Test-DocPhrases 'TESTING.md' ([string]$testingDoc) $testingDocPhrases
}
elseif ($env:TOOLS_GATE_STANDALONE -eq '1') {
    Write-Host "  SKIPPED  TESTING.md phrases: a standalone run has no crgolden workspace"
}
else {
    $failures.Add("DOC  no crgolden workspace: set CRGOLDEN_WORKSPACE (gate.ps1 -Workspace does) or run gate.ps1 -Standalone")
    Write-Host "  FAIL  TESTING.md is readable" -ForegroundColor Red
}

Write-Host ""
if ($failures.Count -eq 0) {
    Write-Host "ALL CHECKS PASSED" -ForegroundColor Green
}
else {
    Write-Host "$($failures.Count) FAILURE(S):" -ForegroundColor Red
    foreach ($f in $failures) { Write-Host "  $f" -ForegroundColor Red }
}

if ($KeepFixtures) {
    Write-Host "`nFixtures kept at $fixtureRoot"
}
else {
    Remove-FixtureTree $fixtureRoot
}

if ($failures.Count -gt 0) { exit 1 }

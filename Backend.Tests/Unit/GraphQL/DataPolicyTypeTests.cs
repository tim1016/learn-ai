using Backend.GraphQL.Types;

namespace Backend.Tests.Unit.GraphQL;

/// <summary>
/// Unit tests for the PR B GraphQL DataPolicy + Engine types. Verifies the
/// canonical JSON → record parse, the legacy / corrupt null fallback, and the
/// Source→Engine mapping shared by the BacktestRun resolver.
/// </summary>
public class DataPolicyTypeTests
{
    private const string CanonicalJson = """
    {
      "source": "polygon",
      "symbol": "SPY",
      "adjusted": true,
      "session": "regular",
      "input_bars": { "timespan": "minute", "multiplier": 1 },
      "strategy_bars": { "timespan": "minute", "multiplier": 15 },
      "timestamp_policy": "bar_close_ms_utc",
      "timezone": "America/New_York",
      "provider_kind": "live",
      "fixture_id": null,
      "fixture_sha256": null
    }
    """;

    [Fact]
    public void TryParse_CanonicalJson_RoundtripsAllFields()
    {
        var dp = DataPolicyType.TryParse(CanonicalJson);

        Assert.NotNull(dp);
        Assert.Equal("polygon", dp!.Source);
        Assert.Equal("SPY", dp.Symbol);
        Assert.True(dp.Adjusted);
        Assert.Equal("regular", dp.Session);
        Assert.Equal("minute", dp.InputBars.Timespan);
        Assert.Equal(1, dp.InputBars.Multiplier);
        Assert.Equal("minute", dp.StrategyBars.Timespan);
        Assert.Equal(15, dp.StrategyBars.Multiplier);
        Assert.Equal("bar_close_ms_utc", dp.TimestampPolicy);
        Assert.Equal("America/New_York", dp.Timezone);
        Assert.Equal("live", dp.ProviderKind);
        Assert.Null(dp.FixtureId);
        Assert.Null(dp.FixtureSha256);
    }

    [Theory]
    [InlineData(null)]
    [InlineData("")]
    [InlineData("   ")]
    public void TryParse_NullOrEmpty_ReturnsNull(string? input)
    {
        Assert.Null(DataPolicyType.TryParse(input));
    }

    [Fact]
    public void TryParse_MalformedJson_ReturnsNull()
    {
        // Corrupt rows must surface as nullable instead of erroring the whole
        // history query — the field is intentionally nullable on the schema.
        Assert.Null(DataPolicyType.TryParse("{not-json}"));
    }

    [Theory]
    [InlineData("engine", Engine.PYTHON)]
    [InlineData("strategy-lab", Engine.PYTHON)]
    [InlineData("lean-sidecar", Engine.LEAN)]
    [InlineData("unknown-future-source", Engine.PYTHON)]
    public void EngineExtensions_FromSource_MapsCorrectly(string source, Engine expected)
    {
        Assert.Equal(expected, EngineExtensions.FromSource(source));
    }
}

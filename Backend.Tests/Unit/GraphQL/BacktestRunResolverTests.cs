using Backend.GraphQL.Resolvers;
using Backend.GraphQL.Types;
using Backend.Models.MarketData;

namespace Backend.Tests.Unit.GraphQL;

/// <summary>
/// Tests for the derived <c>engine</c> and <c>dataPolicy</c> fields exposed by
/// <see cref="BacktestRunResolver"/> on <see cref="StrategyExecution"/>. The
/// derivation is intentionally tested at the resolver-method level (not via
/// IRequestExecutor) so the assertions stay tight against the schema-neutral
/// mapping logic; integration through the schema is covered by existing
/// query-level tests.
/// </summary>
public class BacktestRunResolverTests
{
    private const string CanonicalDataPolicyJson = """
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

    [Theory]
    [InlineData("engine", Engine.PYTHON)]
    [InlineData("lean-sidecar", Engine.LEAN)]
    [InlineData("strategy-lab", Engine.PYTHON)]
    public void GetEngine_DerivesFromSourceColumn(string source, Engine expected)
    {
        var resolver = new BacktestRunResolver();
        var execution = new StrategyExecution { Source = source };

        Assert.Equal(expected, resolver.GetEngine(execution));
    }

    [Fact]
    public void GetDataPolicy_NewRow_ReturnsParsedRecord()
    {
        var resolver = new BacktestRunResolver();
        var execution = new StrategyExecution { DataPolicyJson = CanonicalDataPolicyJson };

        var dp = resolver.GetDataPolicy(execution);

        Assert.NotNull(dp);
        Assert.Equal("SPY", dp!.Symbol);
        Assert.Equal("minute", dp.StrategyBars.Timespan);
        Assert.Equal(15, dp.StrategyBars.Multiplier);
    }

    [Theory]
    [InlineData(null)]
    [InlineData("")]
    public void GetDataPolicy_LegacyRow_ReturnsNull(string? json)
    {
        var resolver = new BacktestRunResolver();
        var execution = new StrategyExecution { DataPolicyJson = json };

        // Legacy rows predate the DataPolicyJson column; the resolver must
        // return null so the GraphQL field is nullable rather than erroring.
        Assert.Null(resolver.GetDataPolicy(execution));
    }

    [Fact]
    public void GetDataPolicy_MalformedJson_ReturnsNull()
    {
        var resolver = new BacktestRunResolver();
        var execution = new StrategyExecution { DataPolicyJson = "{corrupt" };

        Assert.Null(resolver.GetDataPolicy(execution));
    }
}

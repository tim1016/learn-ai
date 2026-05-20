using System.Text.Json;
using Backend.Models.MarketData;
using Backend.Services;

namespace Backend.Tests.Unit.Services;

/// <summary>
/// PR B (2026-05-19) Phase 4 Task 4.1 — equivalence-gate tests. The seven
/// scenarios encoded here mirror the plan's required test set verbatim;
/// they pin the gate's failure shape (specific tokens in
/// <see cref="CompatibilityResult.Mismatches"/>) and the soft-brokerage
/// semantics from spec § 9.2.
/// </summary>
public class RunCompareServiceTests
{
    private const string CanonicalDataPolicyJson = """
{"source":"polygon","symbol":"SPY","adjusted":true,"session":"regular","input_bars":{"timespan":"minute","multiplier":1},"strategy_bars":{"timespan":"minute","multiplier":15},"timestamp_policy":"bar_close_ms_utc","timezone":"America/New_York","provider_kind":"live","fixture_id":null,"fixture_sha256":null}
""";

    [Fact]
    public void EvaluateCompatibility_IdenticalAllFields_ReturnsCompatibleTrue()
    {
        var left = MakeRow();
        var right = MakeRow();
        var sut = new RunCompareService();

        var result = sut.EvaluateCompatibility(left, right);

        Assert.True(result.Compatible);
        Assert.Empty(result.Mismatches);
    }

    [Fact]
    public void EvaluateCompatibility_DifferentStartingCash_FailsGate()
    {
        var left = MakeRow(cash: 100_000m);
        var right = MakeRow(cash: 50_000m);
        var sut = new RunCompareService();

        var result = sut.EvaluateCompatibility(left, right);

        Assert.False(result.Compatible);
        Assert.Contains("starting_cash", result.Mismatches);
    }

    [Fact]
    public void EvaluateCompatibility_DifferentStrategyBars_FailsGate()
    {
        var left = MakeRow(strategyBars: ("minute", 15));
        var right = MakeRow(strategyBars: ("minute", 30));
        var sut = new RunCompareService();

        var result = sut.EvaluateCompatibility(left, right);

        Assert.False(result.Compatible);
        Assert.Contains("strategy_bars", result.Mismatches);
    }

    [Fact]
    public void EvaluateCompatibility_DifferentFillMode_FailsGate()
    {
        var left = MakeRow(fillMode: "signal_bar_close");
        var right = MakeRow(fillMode: "next_bar_open");
        var sut = new RunCompareService();

        var result = sut.EvaluateCompatibility(left, right);

        Assert.False(result.Compatible);
        Assert.Contains("fill_mode", result.Mismatches);
    }

    [Fact]
    public void EvaluateCompatibility_BrokerageSoftMatch_OneDefaultOneIBKR_PassesWithInfo()
    {
        var left = MakeRow(brokerage: "algorithm_default");
        var right = MakeRow(brokerage: "interactive_brokers");
        var sut = new RunCompareService();

        var result = sut.EvaluateCompatibility(left, right);

        // Per spec § 9.2: soft when either side is "algorithm_default" or null.
        Assert.True(result.Compatible);
        Assert.DoesNotContain("brokerage_policy", result.Mismatches);
        Assert.Contains("brokerage_policy", result.InformationalDifferences);
    }

    [Fact]
    public void EvaluateCompatibility_BothNonDefaultBrokeragesDiffer_FailsGate()
    {
        var left = MakeRow(brokerage: "interactive_brokers");
        var right = MakeRow(brokerage: "tradier"); // hypothetical second non-default
        var sut = new RunCompareService();

        var result = sut.EvaluateCompatibility(left, right);

        Assert.False(result.Compatible);
        Assert.Contains("brokerage_policy", result.Mismatches);
    }

    [Fact]
    public void EvaluateCompatibility_MissingDataPolicy_FailsGate()
    {
        var left = MakeRow(dataPolicyJson: null);
        var right = MakeRow();
        var sut = new RunCompareService();

        var result = sut.EvaluateCompatibility(left, right);

        Assert.False(result.Compatible);
        Assert.Contains("data_policy_missing", result.Mismatches);
    }

    // -------------------------------------------------------------------
    // Helpers
    // -------------------------------------------------------------------

    private static StrategyExecution MakeRow(
        string symbol = "SPY",
        decimal cash = 100_000m,
        string fillMode = "signal_bar_close",
        decimal commission = 0m,
        string? brokerage = "algorithm_default",
        (string Timespan, int Multiplier)? strategyBars = null,
        string? dataPolicyJson = "")
    {
        // Defaults: CanonicalDataPolicyJson unless caller passes a literal null
        // (which simulates a legacy row).  An explicit override of strategyBars
        // patches the JSON in place.
        string? policy;
        if (dataPolicyJson == "")
        {
            policy = strategyBars.HasValue
                ? BuildDataPolicyJson(symbol, strategyBars.Value.Timespan, strategyBars.Value.Multiplier)
                : CanonicalDataPolicyJson;
        }
        else
        {
            policy = dataPolicyJson;
        }

        return new StrategyExecution
        {
            Id = 0,
            TickerId = 1,
            StrategyName = "spy_ema_crossover",
            StartDate = "2025-01-13",
            EndDate = "2025-01-17",
            Timespan = "minute",
            Multiplier = 1,
            InitialCash = cash,
            FillMode = fillMode,
            CommissionPerOrder = commission,
            BrokeragePolicy = brokerage,
            DataPolicyJson = policy,
            Source = "engine",
        };
    }

    private static string BuildDataPolicyJson(string symbol, string sbTimespan, int sbMultiplier)
    {
        var dp = new
        {
            source = "polygon",
            symbol,
            adjusted = true,
            session = "regular",
            input_bars = new { timespan = "minute", multiplier = 1 },
            strategy_bars = new { timespan = sbTimespan, multiplier = sbMultiplier },
            timestamp_policy = "bar_close_ms_utc",
            timezone = "America/New_York",
            provider_kind = "live",
            fixture_id = (string?)null,
            fixture_sha256 = (string?)null,
        };
        return JsonSerializer.Serialize(dp);
    }
}

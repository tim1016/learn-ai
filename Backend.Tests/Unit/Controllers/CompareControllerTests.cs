using System.Net;
using System.Text;
using System.Text.Json;
using Backend.Controllers;
using Backend.Models.Compare;
using Backend.Models.MarketData;
using Backend.Services;
using Backend.Tests.Helpers;
using Microsoft.Extensions.Logging.Abstractions;

namespace Backend.Tests.Unit.Controllers;

/// <summary>
/// PR B (2026-05-19) Phase 4 Task 4.4 — controller unit tests. The
/// minimal-API handler delegates to <see cref="CompareController.BuildCompareAsync"/>;
/// each scenario in this file drives that method directly with an in-memory
/// <see cref="Backend.Data.AppDbContext"/> + a <see cref="FakeHttpMessageHandler"/>
/// shaped to mimic the Python ``reconcile-trades`` response, so the test
/// suite stays self-contained (no real HTTP, no ASP.NET pipeline).
/// </summary>
public class CompareControllerTests
{
    private const string CanonicalDataPolicyJson = """
{"source":"polygon","symbol":"SPY","adjusted":true,"session":"regular","input_bars":{"timespan":"minute","multiplier":1},"strategy_bars":{"timespan":"minute","multiplier":15},"timestamp_policy":"bar_close_ms_utc","timezone":"America/New_York","provider_kind":"live","fixture_id":null,"fixture_sha256":null}
""";

    private static readonly string EmptyTradeDiffJson = JsonSerializer.Serialize(new
    {
        matched_pairs = Array.Empty<object>(),
        python_only = Array.Empty<object>(),
        lean_only = Array.Empty<object>(),
        first_divergence = (object?)null,
    });

    [Fact]
    public async Task Compare_HappyPath_ReturnsCompatibleResponse()
    {
        var db = TestDbContextFactory.Create();
        var leftRow = BuildRow(db, runId: 1, dataPolicyJson: CanonicalDataPolicyJson);
        var rightRow = BuildRow(db, runId: 2, dataPolicyJson: CanonicalDataPolicyJson);

        var python = BuildPythonClient(EmptyTradeDiffJson);
        var result = await CompareController.BuildCompareAsync(
            leftRow.Id, rightRow.Id, db, new RunCompareService(), python,
            NullLogger.Instance, CancellationToken.None);

        Assert.NotNull(result);
        Assert.True(result!.Compatible);
        Assert.Empty(result.Mismatches);
        Assert.Equal("PYTHON", result.Left.Engine);
    }

    [Fact]
    public async Task Compare_IncompatibleDataPolicy_ReturnsCompatibleFalse()
    {
        var db = TestDbContextFactory.Create();
        var leftRow = BuildRow(db, runId: 1, dataPolicyJson: CanonicalDataPolicyJson);
        var rightRow = BuildRow(db, runId: 2, dataPolicyJson: BuildPolicyJson(strategyMultiplier: 30));

        var python = BuildPythonClient(EmptyTradeDiffJson);
        var result = await CompareController.BuildCompareAsync(
            leftRow.Id, rightRow.Id, db, new RunCompareService(), python,
            NullLogger.Instance, CancellationToken.None);

        Assert.NotNull(result);
        Assert.False(result!.Compatible);
        Assert.Contains("strategy_bars", result.Mismatches);
    }

    [Fact]
    public async Task Compare_FirstDivergence_PopulatedWhenTradeMismatch()
    {
        var db = TestDbContextFactory.Create();
        var leftRow = BuildRow(db, runId: 1, dataPolicyJson: CanonicalDataPolicyJson);
        var rightRow = BuildRow(db, runId: 2, dataPolicyJson: CanonicalDataPolicyJson);

        var pythonBody = JsonSerializer.Serialize(new
        {
            matched_pairs = new[]
            {
                new
                {
                    trade_number = 1,
                    entry_ts_delta_ms = 0,
                    exit_ts_delta_ms = 0,
                    entry_price_delta = "0.00",
                    exit_price_delta = "0.02",
                    qty_delta = "0",
                    pnl_delta = "1.70",
                    category = "fill_price_drift",
                },
            },
            python_only = Array.Empty<object>(),
            lean_only = Array.Empty<object>(),
            first_divergence = new
            {
                trade_index = 0,
                what = "exit_price_delta",
                category = "fill_price_drift",
                left_value = "421.50",
                right_value = "421.52",
            },
        });

        var python = BuildPythonClient(pythonBody);
        var result = await CompareController.BuildCompareAsync(
            leftRow.Id, rightRow.Id, db, new RunCompareService(), python,
            NullLogger.Instance, CancellationToken.None);

        Assert.NotNull(result);
        Assert.NotNull(result!.FirstDivergence);
        Assert.Equal("fill_price_drift", result.FirstDivergence!.Category);
        Assert.Equal(0, result.FirstDivergence.TradeIndex);
    }

    [Fact]
    public async Task Compare_StateTraceAsymmetry_OnlyOneSideHasStateCsv_ReturnsFalse()
    {
        // v1 contract: ``DetectStateTrace`` always returns false because
        // the workspace-path column isn't wired yet (Phase 5).  The
        // important behavioral property is no exception is thrown when
        // one side has artifacts and the other doesn't — this test
        // ensures the response is well-formed in that asymmetric case.
        var db = TestDbContextFactory.Create();
        var leftRow = BuildRow(db, runId: 1, dataPolicyJson: CanonicalDataPolicyJson);
        var rightRow = BuildRow(db, runId: 2, dataPolicyJson: CanonicalDataPolicyJson, source: "lean-sidecar", leanRunId: "lean_only");

        var python = BuildPythonClient(EmptyTradeDiffJson);
        var result = await CompareController.BuildCompareAsync(
            leftRow.Id, rightRow.Id, db, new RunCompareService(), python,
            NullLogger.Instance, CancellationToken.None);

        Assert.NotNull(result);
        Assert.False(result!.StateTraceAvailable);
    }

    [Fact]
    public async Task Compare_UnmatchedTrades_AppearsInPythonOnlyOrLeanOnly()
    {
        var db = TestDbContextFactory.Create();
        var leftRow = BuildRow(db, runId: 1, dataPolicyJson: CanonicalDataPolicyJson);
        var rightRow = BuildRow(db, runId: 2, dataPolicyJson: CanonicalDataPolicyJson);

        var pythonBody = JsonSerializer.Serialize(new
        {
            matched_pairs = new[]
            {
                new
                {
                    trade_number = 1,
                    entry_ts_delta_ms = 0,
                    exit_ts_delta_ms = 0,
                    entry_price_delta = "0.00",
                    exit_price_delta = "0.00",
                    qty_delta = "0",
                    pnl_delta = "0.00",
                    category = "matched",
                },
            },
            python_only = new[]
            {
                new
                {
                    trade_number = 7,
                    entry_ms_utc = 1736773800000L,
                    exit_ms_utc = 1736775000000L,
                    entry_price = "100.00",
                    exit_price = "100.50",
                    quantity = "100",
                    pnl = "50.00",
                },
            },
            lean_only = Array.Empty<object>(),
            first_divergence = (object?)null,
        });

        var python = BuildPythonClient(pythonBody);
        var result = await CompareController.BuildCompareAsync(
            leftRow.Id, rightRow.Id, db, new RunCompareService(), python,
            NullLogger.Instance, CancellationToken.None);

        Assert.NotNull(result);
        Assert.Single(result!.TradeDiff.PythonOnly);
        Assert.Empty(result.TradeDiff.LeanOnly);
        Assert.Equal(7, result.TradeDiff.PythonOnly[0].TradeNumber);
    }

    [Fact]
    public async Task Compare_MissingRun_ReturnsNull()
    {
        // Missing-row contract: ``BuildCompareAsync`` returns null and the
        // minimal-API wrapper translates that to a 404.
        var db = TestDbContextFactory.Create();
        var python = BuildPythonClient(EmptyTradeDiffJson);
        var result = await CompareController.BuildCompareAsync(
            left: 999, right: 998, db, new RunCompareService(), python,
            NullLogger.Instance, CancellationToken.None);

        Assert.Null(result);
    }

    // -------------------------------------------------------------------
    // Helpers
    // -------------------------------------------------------------------

    private static StrategyExecution BuildRow(
        Backend.Data.AppDbContext db,
        int runId,
        string dataPolicyJson,
        string source = "engine",
        string? leanRunId = null)
    {
        var ticker = new Ticker { Symbol = "SPY", Name = "SPY", Market = "stocks" };
        db.Tickers.Add(ticker);
        db.SaveChanges();

        var execution = new StrategyExecution
        {
            TickerId = ticker.Id,
            StrategyName = "spy_ema_crossover",
            Parameters = "{}",
            StartDate = "2025-01-13",
            EndDate = "2025-01-17",
            Timespan = "minute",
            Multiplier = 1,
            InitialCash = 100_000m,
            FillMode = "signal_bar_close",
            CommissionPerOrder = 0m,
            BrokeragePolicy = "algorithm_default",
            DataPolicyJson = dataPolicyJson,
            Source = source,
            LeanRunId = leanRunId,
        };
        db.StrategyExecutions.Add(execution);
        db.SaveChanges();
        return execution;
    }

    private static string BuildPolicyJson(int strategyMultiplier)
    {
        return $$"""
{"source":"polygon","symbol":"SPY","adjusted":true,"session":"regular","input_bars":{"timespan":"minute","multiplier":1},"strategy_bars":{"timespan":"minute","multiplier":{{strategyMultiplier}}},"timestamp_policy":"bar_close_ms_utc","timezone":"America/New_York","provider_kind":"live","fixture_id":null,"fixture_sha256":null}
""";
    }

    private static HttpClient BuildPythonClient(string responseBody)
    {
        var handler = new FakeHttpMessageHandler(HttpStatusCode.OK, responseBody);
        return new HttpClient(handler)
        {
            BaseAddress = new Uri("http://python-service.test/"),
        };
    }
}

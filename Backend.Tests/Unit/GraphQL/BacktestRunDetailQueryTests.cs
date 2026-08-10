using Backend.GraphQL;
using Backend.Models.MarketData;
using Backend.Tests.Helpers;
using Microsoft.Extensions.Logging.Abstractions;

namespace Backend.Tests.Unit.GraphQL;

public class BacktestRunDetailQueryTests
{
    [Fact]
    public async Task GetBacktestRun_LargeLedger_ReturnsBoundedRecentTradeEvidence()
    {
        await using var db = TestDbContextFactory.Create();
        var start = new DateTime(2026, 1, 2, 14, 30, 0, DateTimeKind.Utc);
        var execution = new StrategyExecution
        {
            Ticker = new Ticker { Symbol = "SPY", Name = "SPY", Market = "stocks" },
            Source = "engine",
            StrategyName = "deployment_validation",
            TotalTrades = 501,
            Trades = Enumerable.Range(0, 501)
                .Select(index => new BacktestTrade
                {
                    TradeType = "Buy",
                    EntryTimestamp = start.AddMinutes(index),
                    ExitTimestamp = start.AddMinutes(index + 1),
                    EntryPrice = 500m,
                    ExitPrice = 501m,
                    Quantity = 1m,
                    PnL = 1m,
                })
                .ToList(),
        };
        db.StrategyExecutions.Add(execution);
        await db.SaveChangesAsync();

        var detail = await new BacktestRunDetailQuery().GetBacktestRun(
            execution.Id,
            db,
            NullLogger<BacktestRunDetailQuery>.Instance,
            CancellationToken.None);

        Assert.NotNull(detail);
        Assert.True(detail.TradesTruncated);
        Assert.Equal(501, detail.TotalTrades);
        Assert.Equal(500, detail.Trades.Count);
        Assert.Equal(start.AddMinutes(1), DateTimeOffset.FromUnixTimeMilliseconds(detail.Trades[0].EntryTimestamp).UtcDateTime);
        Assert.Equal(start.AddMinutes(500), DateTimeOffset.FromUnixTimeMilliseconds(detail.Trades[^1].EntryTimestamp).UtcDateTime);
    }

    [Fact]
    public void FromExecution_ExposesOperatorRequestedEngineForHistoryRehydration()
    {
        var execution = new StrategyExecution
        {
            Ticker = new Ticker { Symbol = "SPY", Name = "SPY", Market = "stocks" },
            Source = "engine",
            RequestedEngine = "both",
            StrategyName = "ema_crossover",
            Parameters = """{"fast":8,"slow":21}""",
            CommissionPerOrder = 0.35m,
        };

        var detail = BacktestRunDetailType.FromExecution(execution, [], NullLogger.Instance);

        Assert.Equal("both", detail.RequestedEngine);
        Assert.Equal("""{"fast":8,"slow":21}""", detail.Parameters);
        Assert.Equal(0.35m, detail.CommissionPerOrder);
    }

    [Fact]
    public void FromExecution_PreservesMissingRequestedEngineForLegacyFallback()
    {
        var execution = new StrategyExecution
        {
            Ticker = new Ticker { Symbol = "SPY", Name = "SPY", Market = "stocks" },
            Source = "lean-sidecar",
            StrategyName = "ema_crossover",
            RequestedEngine = null,
        };

        var detail = BacktestRunDetailType.FromExecution(execution, [], NullLogger.Instance);

        Assert.Null(detail.RequestedEngine);
    }

    [Fact]
    public void FromExecution_LeanAnalysis_PreservesCompleteJsonEnvelope()
    {
        const string analysis = """
            [{
              "name": "FlatEquityCurveAnalysis",
              "issue": "The curve is flat.",
              "sample": [{ "start": 1700000000000, "end": 1700086400000, "trading_days": 2 }],
              "solutions": ["Check warm-up.", "Check subscriptions."]
            }]
            """;
        var execution = new StrategyExecution
        {
            Ticker = new Ticker { Symbol = "SPY", Name = "SPY", Market = "stocks" },
            Source = "lean-sidecar",
            StrategyName = "ema_crossover",
            LeanAnalysisJson = analysis,
        };

        var detail = BacktestRunDetailType.FromExecution(execution, [], NullLogger.Instance);

        Assert.Equal(analysis, detail.LeanAnalysisJson);
    }

    [Fact]
    public void FromExecution_StrictEquityEnvelope_ParsesBothCurveIdentities()
    {
        var execution = new StrategyExecution
        {
            Ticker = new Ticker { Symbol = "SPY", Name = "SPY", Market = "stocks" },
            Source = "engine",
            StrategyName = "ema_crossover",
            EquityCurveJson = """
            {
              "schema_version": 2,
              "mark_to_market": {
                "cadence": "strategy_bar_close",
                "downsample": { "raw_points": 2, "kept_points": 2 },
                "points": [
                  { "t": 1700000000000, "e": 100000.12 },
                  { "t": 1700000060000, "e": 100010.34 }
                ]
              },
              "realized": {
                "cadence": "trade_exit",
                "downsample": { "raw_points": 2, "kept_points": 2 },
                "points": [
                  { "t": 1700000000000, "e": 100000.00 },
                  { "t": 1700000060000, "e": 100010.34 }
                ]
              }
            }
            """,
        };

        var detail = BacktestRunDetailType.FromExecution(execution, [], NullLogger.Instance);

        Assert.NotNull(detail.EquityCurve);
        Assert.Equal(2, detail.EquityCurve.SchemaVersion);
        Assert.NotNull(detail.EquityCurve.MarkToMarket);
        Assert.NotNull(detail.EquityCurve.Realized);
        Assert.Equal("strategy_bar_close", detail.EquityCurve.MarkToMarket.Cadence);
        Assert.Equal("trade_exit", detail.EquityCurve.Realized.Cadence);
        Assert.Equal(2, detail.EquityCurve.MarkToMarket.RawPoints);
        Assert.Equal(2, detail.EquityCurve.Realized.Points.Count);
        Assert.Equal(1_700_000_000_000, detail.EquityCurve.MarkToMarket.Points[0].T);
        Assert.Equal(100000.12m, detail.EquityCurve.MarkToMarket.Points[0].E);
    }

    [Fact]
    public void FromExecution_LegacyRunWithNoEquityEnvelope_ReturnsEmptyCurve()
    {
        var execution = new StrategyExecution
        {
            Ticker = new Ticker { Symbol = "SPY", Name = "SPY", Market = "stocks" },
            Source = "engine",
            StrategyName = "ema_crossover",
        };

        var detail = BacktestRunDetailType.FromExecution(execution, [], NullLogger.Instance);

        Assert.Null(detail.EquityCurve);
    }

    [Fact]
    public void FromExecution_LegacyBareEquityCurve_PreservesMarkToMarketAndMarksRealizedUnavailable()
    {
        var execution = new StrategyExecution
        {
            Ticker = new Ticker { Symbol = "SPY", Name = "SPY", Market = "stocks" },
            Source = "engine",
            StrategyName = "ema_crossover",
            EquityCurveJson = """
            {
              "cadence": "strategy_bar_close",
              "downsample": { "raw_points": 2, "kept_points": 2 },
              "points": [
                { "t": 1700000000000, "e": 100000.00 },
                { "t": 1700000060000, "e": 100010.00 }
              ]
            }
            """,
        };

        var detail = BacktestRunDetailType.FromExecution(execution, [], NullLogger.Instance);

        Assert.Equal(1, detail.EquityCurve!.SchemaVersion);
        Assert.Equal("strategy_bar_close", detail.EquityCurve.MarkToMarket!.Cadence);
        Assert.Equal(2, detail.EquityCurve.MarkToMarket.Points.Count);
        Assert.Equal(100010.00m, detail.EquityCurve.MarkToMarket.Points[1].E);
        Assert.Equal(
            "Realized equity was not recorded for this legacy run.",
            detail.EquityCurve.Realized!.Error);
    }

    [Fact]
    public void FromExecution_CorruptEquityEnvelope_ReturnsUnreadableReceipt()
    {
        var execution = new StrategyExecution
        {
            Ticker = new Ticker { Symbol = "SPY", Name = "SPY", Market = "stocks" },
            Source = "engine",
            StrategyName = "ema_crossover",
            EquityCurveJson = "{ nope",
        };

        var detail = BacktestRunDetailType.FromExecution(execution, [], NullLogger.Instance);

        Assert.NotNull(detail.EquityCurve);
        Assert.Equal("Equity curve envelope unreadable.", detail.EquityCurve.Error);
        Assert.Null(detail.EquityCurve.Realized);
    }

    [Fact]
    public void FromExecution_RejectsARealizedCurveWithTheWrongCadence()
    {
        var execution = new StrategyExecution
        {
            Ticker = new Ticker { Symbol = "SPY", Name = "SPY", Market = "stocks" },
            Source = "engine",
            StrategyName = "ema_crossover",
            EquityCurveJson = """
            {
              "schema_version": 2,
              "mark_to_market": { "cadence": "strategy_bar_close", "points": [] },
              "realized": { "cadence": "strategy_bar_close", "points": [] }
            }
            """,
        };

        var detail = BacktestRunDetailType.FromExecution(execution, [], NullLogger.Instance);

        Assert.Equal("Realized curve has an unsupported cadence.", detail.EquityCurve!.Realized!.Error);
    }

    [Fact]
    public void FromExecution_ValidValidationAnalyticsEnvelope_ParsesTypedSections()
    {
        var execution = new StrategyExecution
        {
            Ticker = new Ticker { Symbol = "SPY", Name = "SPY", Market = "stocks" },
            Source = "engine",
            StrategyName = "ema_crossover",
            ValidationAnalyticsJson = """
            {
              "schema_version": 1,
              "computed_at_ms": 1783958400000,
              "engine": "python",
              "analytics": {
                "horizons": [
                  {
                    "key": "2w", "label": "2 weeks",
                    "start_ms_utc": 1700000000000, "end_ms_utc": 1701000000000,
                    "has_full_coverage": true, "net_return": 0.012,
                    "trade_count": 3, "win_rate": 0.66, "profit_factor": 1.8
                  }
                ],
                "timing_cells": [
                  {
                    "weekday": 0, "weekday_label": "Mon", "hour_et": 10,
                    "trade_count": 2, "win_rate": 0.5, "average_return": 0.004
                  }
                ],
                "seasonality": [
                  { "month": 1, "month_label": "Jan", "observation_count": 2, "median_compounded_return": 0.01 }
                ],
                "rolling_trade_stability": [
                  { "trade_number": 20, "end_ms_utc": 1700500000000, "window_size": 20, "average_return": 0.002, "win_rate": 0.6 }
                ]
              }
            }
            """,
        };

        var detail = BacktestRunDetailType.FromExecution(execution, [], NullLogger.Instance);

        Assert.NotNull(detail.ValidationAnalytics);
        Assert.Null(detail.ValidationAnalytics.Error);
        Assert.Equal(1, detail.ValidationAnalytics.SchemaVersion);
        Assert.Equal(1_783_958_400_000, detail.ValidationAnalytics.ComputedAtMs);
        Assert.Equal("python", detail.ValidationAnalytics.Engine);
        var horizon = Assert.Single(detail.ValidationAnalytics.Horizons);
        Assert.Equal("2w", horizon.Key);
        Assert.Equal(1_700_000_000_000, horizon.StartMsUtc);
        Assert.Equal(0.012, horizon.NetReturn);
        var cell = Assert.Single(detail.ValidationAnalytics.TimingCells);
        Assert.Equal(10, cell.HourEt);
        var month = Assert.Single(detail.ValidationAnalytics.Seasonality);
        Assert.Equal("Jan", month.MonthLabel);
        var rolling = Assert.Single(detail.ValidationAnalytics.RollingTradeStability);
        Assert.Equal(20, rolling.WindowSize);
    }

    [Fact]
    public void FromExecution_MissingValidationAnalytics_ReturnsNull()
    {
        var execution = new StrategyExecution
        {
            Ticker = new Ticker { Symbol = "SPY", Name = "SPY", Market = "stocks" },
            Source = "engine",
            StrategyName = "ema_crossover",
        };

        var detail = BacktestRunDetailType.FromExecution(execution, [], NullLogger.Instance);

        Assert.Null(detail.ValidationAnalytics);
    }

    [Fact]
    public void FromExecution_CorruptValidationAnalytics_ReturnsUnreadableReceipt()
    {
        var execution = new StrategyExecution
        {
            Ticker = new Ticker { Symbol = "SPY", Name = "SPY", Market = "stocks" },
            Source = "engine",
            StrategyName = "ema_crossover",
            ValidationAnalyticsJson = "{ nope",
        };

        var detail = BacktestRunDetailType.FromExecution(execution, [], NullLogger.Instance);

        Assert.NotNull(detail.ValidationAnalytics);
        Assert.Equal("Validation analytics envelope unreadable.", detail.ValidationAnalytics.Error);
        Assert.Empty(detail.ValidationAnalytics.Horizons);
    }

    [Fact]
    public void FromExecution_LeanRunWithStoredLeanStats_UsesLeanKpis()
    {
        var execution = new StrategyExecution
        {
            Ticker = new Ticker { Symbol = "SPY", Name = "SPY", Market = "stocks" },
            Source = "lean-sidecar",
            StrategyName = "ema_crossover",
            MaxDrawdown = 0m,
            SharpeRatio = 0m,
            SortinoRatio = 0m,
            ProfitFactor = 0m,
            LeanStatisticsJson = """
            {
              "portfolio": {
                "drawdown": 0.123,
                "sharpe_ratio": 1.45,
                "sortino_ratio": 2.10
              },
              "trade": {
                "profit_factor": 2.35
              }
            }
            """,
        };

        var detail = BacktestRunDetailType.FromExecution(execution, [], NullLogger.Instance);

        Assert.Equal(0.123m, detail.MaxDrawdown);
        Assert.Equal(1.45m, detail.SharpeRatio);
        Assert.Equal(2.10m, detail.SortinoRatio);
        Assert.Equal(2.35m, detail.ProfitFactor);
    }

    [Fact]
    public void FromExecution_RecordedDocumentationContext_PreservesTheProducerAndContract()
    {
        var execution = new StrategyExecution
        {
            Ticker = new Ticker { Symbol = "SPY", Name = "SPY", Market = "stocks" },
            Source = "lean-sidecar",
            StrategyName = "ema_crossover",
            MetricDocumentationJson = """
            [{
              "metric_id": "sharpe",
              "variant_id": "sharpe.lean_native.v1",
              "producer": "lean_native",
              "contract_id": "lean-statistics-oracle-v1"
            }]
            """,
        };

        var detail = BacktestRunDetailType.FromExecution(execution, [], NullLogger.Instance);

        var context = Assert.Single(detail.MetricDocumentation);
        Assert.Equal("sharpe", context.MetricId);
        Assert.Equal("sharpe.lean_native.v1", context.VariantId);
        Assert.Equal("lean_native", context.Producer);
        Assert.Equal("lean-statistics-oracle-v1", context.ContractId);
        Assert.Equal("recorded", context.ContractProvenance);
    }

    [Fact]
    public void FromExecution_LegacyLeanSharpe_InfersTheDisplayedNativeVariant()
    {
        var execution = new StrategyExecution
        {
            Ticker = new Ticker { Symbol = "SPY", Name = "SPY", Market = "stocks" },
            Source = "lean-sidecar",
            StrategyName = "ema_crossover",
            LeanStatisticsJson = """{"portfolio":{"sharpe_ratio":1.45}}""",
        };

        var detail = BacktestRunDetailType.FromExecution(execution, [], NullLogger.Instance);

        var context = Assert.Single(detail.MetricDocumentation);
        Assert.Equal("sharpe.lean_native.v1", context.VariantId);
        Assert.Equal("lean_native", context.Producer);
        Assert.Equal("inferred", context.ContractProvenance);
    }

    [Fact]
    public void FromExecution_LegacyPlatformSharpe_InfersThePlatformVariant()
    {
        var execution = new StrategyExecution
        {
            Ticker = new Ticker { Symbol = "SPY", Name = "SPY", Market = "stocks" },
            Source = "engine",
            StrategyName = "ema_crossover",
            SharpeRatio = 1.45m,
        };

        var detail = BacktestRunDetailType.FromExecution(execution, [], NullLogger.Instance);

        var context = Assert.Single(detail.MetricDocumentation);
        Assert.Equal("sharpe.platform.v1", context.VariantId);
        Assert.Equal("platform", context.Producer);
        Assert.Equal("inferred", context.ContractProvenance);
    }

    [Fact]
    public void FromExecution_LeanSharpeAbsent_StillInfersTheNativeVariantNotPlatform()
    {
        // A legacy lean-sidecar row with no portfolio.sharpe_ratio, or a failed
        // LEAN run whose statistics default to null, is still LEAN-produced.
        // Gating inference on leanKpis?.SharpeRatio (instead of Source alone)
        // used to mislabel both as the platform contract.
        var execution = new StrategyExecution
        {
            Ticker = new Ticker { Symbol = "SPY", Name = "SPY", Market = "stocks" },
            Source = "lean-sidecar",
            StrategyName = "ema_crossover",
            LeanStatisticsJson = """{"portfolio":{}}""",
        };

        var detail = BacktestRunDetailType.FromExecution(execution, [], NullLogger.Instance);

        var context = Assert.Single(detail.MetricDocumentation);
        Assert.Equal("sharpe.lean_native.v1", context.VariantId);
        Assert.Equal("lean_native", context.Producer);
        Assert.Equal("inferred", context.ContractProvenance);
    }

    [Fact]
    public void FromExecution_EmptyRecordedDocumentationArray_FallsBackToInference()
    {
        // [] deserializes successfully; .All() is vacuously true on an empty
        // sequence. Without an explicit count check this used to short-circuit
        // to "no contexts" instead of falling through to inference.
        var execution = new StrategyExecution
        {
            Ticker = new Ticker { Symbol = "SPY", Name = "SPY", Market = "stocks" },
            Source = "lean-sidecar",
            StrategyName = "ema_crossover",
            LeanStatisticsJson = """{"portfolio":{"sharpe_ratio":1.45}}""",
            MetricDocumentationJson = "[]",
        };

        var detail = BacktestRunDetailType.FromExecution(execution, [], NullLogger.Instance);

        var context = Assert.Single(detail.MetricDocumentation);
        Assert.Equal("sharpe.lean_native.v1", context.VariantId);
        Assert.Equal("inferred", context.ContractProvenance);
    }

    [Fact]
    public void FromExecution_PartiallyInvalidRecordedDocumentation_DiscardsAllAndFallsBackToInference()
    {
        var execution = new StrategyExecution
        {
            Ticker = new Ticker { Symbol = "SPY", Name = "SPY", Market = "stocks" },
            Source = "lean-sidecar",
            StrategyName = "ema_crossover",
            LeanStatisticsJson = """{"portfolio":{"sharpe_ratio":1.45}}""",
            MetricDocumentationJson = """
            [{
              "metric_id": "sharpe",
              "variant_id": "sharpe.lean_native.v1",
              "producer": "lean_native",
              "contract_id": "lean-statistics-oracle-v1"
            }, {
              "metric_id": "",
              "variant_id": "sortino.lean_native.v1",
              "producer": "lean_native",
              "contract_id": "lean-statistics-oracle-v1"
            }]
            """,
        };

        var detail = BacktestRunDetailType.FromExecution(execution, [], NullLogger.Instance);

        var context = Assert.Single(detail.MetricDocumentation);
        Assert.Equal("inferred", context.ContractProvenance);
    }
}

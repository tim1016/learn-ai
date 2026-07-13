using Backend.GraphQL;
using Backend.Models.MarketData;
using Microsoft.Extensions.Logging.Abstractions;

namespace Backend.Tests.Unit.GraphQL;

public class BacktestRunDetailQueryTests
{
    [Fact]
    public void FromExecution_ValidEquityEnvelope_ParsesPoints()
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
                { "t": 1700000000000, "e": 100000.12 },
                { "t": 1700000060000, "e": 100010.34 }
              ]
            }
            """,
        };

        var detail = BacktestRunDetailType.FromExecution(execution, [], NullLogger.Instance);

        Assert.NotNull(detail.EquityCurve);
        Assert.Equal("strategy_bar_close", detail.EquityCurve.Cadence);
        Assert.Equal(2, detail.EquityCurve.RawPoints);
        Assert.Equal(2, detail.EquityCurve.KeptPoints);
        Assert.Equal(2, detail.EquityCurve.Points.Count);
        Assert.Equal(1_700_000_000_000, detail.EquityCurve.Points[0].T);
        Assert.Equal(100000.12m, detail.EquityCurve.Points[0].E);
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
        Assert.Empty(detail.EquityCurve.Points);
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
}

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
}

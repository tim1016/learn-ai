using Backend.GraphQL;
using Backend.Models.MarketData;

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

        var detail = BacktestRunDetailType.FromExecution(execution, []);

        Assert.Equal(2, detail.EquityCurve.Count);
        Assert.Equal(1_700_000_000_000, detail.EquityCurve[0].T);
        Assert.Equal(100000.12m, detail.EquityCurve[0].E);
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

        var detail = BacktestRunDetailType.FromExecution(execution, []);

        Assert.Empty(detail.EquityCurve);
    }
}

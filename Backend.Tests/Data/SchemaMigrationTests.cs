using Backend.Models.MarketData;

namespace Backend.Tests.Data;

public class SchemaMigrationTests
{
    [Fact]
    public void StrategyExecution_HasLeanRunIdProperty()
    {
        var prop = typeof(StrategyExecution).GetProperty(nameof(StrategyExecution.LeanRunId));
        Assert.NotNull(prop);
        Assert.Equal(typeof(string), prop!.PropertyType);
    }

    [Fact]
    public void BacktestTrade_HasIsSyntheticExitProperty()
    {
        var prop = typeof(BacktestTrade).GetProperty(nameof(BacktestTrade.IsSyntheticExit));
        Assert.NotNull(prop);
        Assert.Equal(typeof(bool), prop!.PropertyType);
    }
}

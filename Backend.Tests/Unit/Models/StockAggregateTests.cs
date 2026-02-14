using Backend.Models.MarketData;

namespace Backend.Tests.Unit.Models;

public class StockAggregateTests
{
    [Fact]
    public void IsValid_WithValidOhlcv_ReturnsTrue()
    {
        var aggregate = new StockAggregate
        {
            Open = 150m, High = 155m, Low = 148m, Close = 153m,
            Volume = 1_000_000m, Timespan = "day"
        };

        Assert.True(aggregate.IsValid());
    }

    [Fact]
    public void IsValid_HighLessThanLow_ReturnsFalse()
    {
        var aggregate = new StockAggregate
        {
            Open = 150m, High = 140m, Low = 148m, Close = 153m,
            Volume = 1_000_000m, Timespan = "day"
        };

        Assert.False(aggregate.IsValid());
    }

    [Fact]
    public void IsValid_NegativeVolume_ReturnsFalse()
    {
        var aggregate = new StockAggregate
        {
            Open = 150m, High = 155m, Low = 148m, Close = 153m,
            Volume = -100m, Timespan = "day"
        };

        Assert.False(aggregate.IsValid());
    }

    [Fact]
    public void IsValid_HighEqualsLow_WhenAllEqual_ReturnsTrue()
    {
        var aggregate = new StockAggregate
        {
            Open = 150m, High = 150m, Low = 150m, Close = 150m,
            Volume = 0m, Timespan = "day"
        };

        Assert.True(aggregate.IsValid());
    }

    [Fact]
    public void IsValid_LowAboveOpen_ReturnsFalse()
    {
        var aggregate = new StockAggregate
        {
            Open = 148m, High = 155m, Low = 150m, Close = 153m,
            Volume = 1_000_000m, Timespan = "day"
        };

        Assert.False(aggregate.IsValid());
    }
}

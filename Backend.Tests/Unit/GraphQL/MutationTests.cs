using Backend.GraphQL;
using Backend.Models.MarketData;
using Backend.Services.Interfaces;
using Moq;

namespace Backend.Tests.Unit.GraphQL;

public class MutationTests
{
    [Fact]
    public async Task FetchStockAggregates_Success_ReturnsSuccessResult()
    {
        var serviceMock = new Mock<IMarketDataService>();
        serviceMock.Setup(s => s.FetchAndStoreAggregatesAsync(
                "AAPL", 1, "day", "2026-01-01", "2026-01-31", default))
            .ReturnsAsync(new List<StockAggregate>
            {
                new() { Open = 150m, High = 155m, Low = 148m, Close = 153m, Volume = 1_000_000m, Timespan = "day" },
                new() { Open = 153m, High = 158m, Low = 151m, Close = 157m, Volume = 900_000m, Timespan = "day" },
            });

        var mutation = new Mutation();
        var result = await mutation.FetchStockAggregates(
            serviceMock.Object, "AAPL", "2026-01-01", "2026-01-31");

        Assert.True(result.Success);
        Assert.Equal("AAPL", result.Ticker);
        Assert.Equal(2, result.Count);
        Assert.Contains("Successfully fetched", result.Message);
    }

    [Fact]
    public async Task FetchStockAggregates_ServiceThrows_ReturnsErrorResult()
    {
        var serviceMock = new Mock<IMarketDataService>();
        serviceMock.Setup(s => s.FetchAndStoreAggregatesAsync(
                It.IsAny<string>(), It.IsAny<int>(), It.IsAny<string>(),
                It.IsAny<string>(), It.IsAny<string>(), It.IsAny<CancellationToken>()))
            .ThrowsAsync(new Exception("Polygon API timeout"));

        var mutation = new Mutation();
        var result = await mutation.FetchStockAggregates(
            serviceMock.Object, "BAD", "2026-01-01", "2026-01-31");

        Assert.False(result.Success);
        Assert.Equal("BAD", result.Ticker);
        Assert.Equal(0, result.Count);
        Assert.Contains("Polygon API timeout", result.Message);
    }

    [Fact]
    public async Task FetchStockAggregates_EmptyResult_ReturnsZeroCount()
    {
        var serviceMock = new Mock<IMarketDataService>();
        serviceMock.Setup(s => s.FetchAndStoreAggregatesAsync(
                It.IsAny<string>(), It.IsAny<int>(), It.IsAny<string>(),
                It.IsAny<string>(), It.IsAny<string>(), It.IsAny<CancellationToken>()))
            .ReturnsAsync(new List<StockAggregate>());

        var mutation = new Mutation();
        var result = await mutation.FetchStockAggregates(
            serviceMock.Object, "AAPL", "2026-01-01", "2026-01-31");

        Assert.True(result.Success);
        Assert.Equal(0, result.Count);
    }
}

using Backend.Models.MarketData;
using Backend.Services.Implementation;
using Backend.Services.Interfaces;
using Backend.Tests.Helpers;
using Microsoft.Extensions.Logging;
using Moq;

namespace Backend.Tests.Unit.Services;

public class MarketDataServiceTests
{
    private readonly Mock<IPolygonService> _polygonServiceMock = new();
    private readonly Mock<ILogger<MarketDataService>> _loggerMock = new();

    private MarketDataService CreateService()
    {
        var context = TestDbContextFactory.Create();
        return new MarketDataService(context, _polygonServiceMock.Object, _loggerMock.Object);
    }

    [Fact]
    public async Task GetOrCreateTickerAsync_NewSymbol_CreatesTicker()
    {
        var service = CreateService();

        var ticker = await service.GetOrCreateTickerAsync("AAPL", "stocks");

        Assert.NotNull(ticker);
        Assert.Equal("AAPL", ticker.Symbol);
        Assert.Equal("stocks", ticker.Market);
        Assert.True(ticker.Active);
    }

    [Fact]
    public async Task GetOrCreateTickerAsync_ExistingSymbol_ReturnsSameTicker()
    {
        var context = TestDbContextFactory.Create();
        var service = new MarketDataService(context, _polygonServiceMock.Object, _loggerMock.Object);

        var first = await service.GetOrCreateTickerAsync("MSFT", "stocks");
        var second = await service.GetOrCreateTickerAsync("MSFT", "stocks");

        Assert.Equal(first.Id, second.Id);
        Assert.Equal(first.Symbol, second.Symbol);
    }

    [Fact]
    public async Task GetOrCreateTickerAsync_DifferentMarkets_CreatesSeparateTickers()
    {
        var context = TestDbContextFactory.Create();
        var service = new MarketDataService(context, _polygonServiceMock.Object, _loggerMock.Object);

        var stocks = await service.GetOrCreateTickerAsync("AAPL", "stocks");
        var crypto = await service.GetOrCreateTickerAsync("AAPL", "crypto");

        Assert.NotEqual(stocks.Id, crypto.Id);
    }

    [Fact]
    public async Task GetOrFetchAggregatesAsync_CacheHit_ReturnsCachedData()
    {
        var context = TestDbContextFactory.Create();
        var service = new MarketDataService(context, _polygonServiceMock.Object, _loggerMock.Object);

        // Seed a ticker and aggregates
        var ticker = new Ticker { Symbol = "AAPL", Name = "AAPL", Market = "stocks" };
        context.Tickers.Add(ticker);
        await context.SaveChangesAsync();

        var aggregate = new StockAggregate
        {
            TickerId = ticker.Id,
            Open = 150m, High = 155m, Low = 148m, Close = 153m,
            Volume = 1_000_000m, Timespan = "day", Multiplier = 1,
            Timestamp = new DateTime(2026, 1, 15, 0, 0, 0, DateTimeKind.Utc)
        };
        context.StockAggregates.Add(aggregate);
        await context.SaveChangesAsync();

        var result = await service.GetOrFetchAggregatesAsync(
            "AAPL", 1, "day", "2026-01-01", "2026-01-31");

        Assert.Single(result);
        Assert.Equal(150m, result[0].Open);
        // Should NOT have called the polygon service
        _polygonServiceMock.Verify(
            p => p.FetchAggregatesAsync(It.IsAny<string>(), It.IsAny<int>(),
                It.IsAny<string>(), It.IsAny<string>(), It.IsAny<string>(),
                It.IsAny<CancellationToken>()),
            Times.Never);
    }
}

using Backend.Models.DTOs.PolygonResponses;
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

    #region GetOrCreateTickerAsync

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

    #endregion

    #region GetOrFetchAggregatesAsync — Cache Hit

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

        Assert.Single(result.Aggregates);
        Assert.Equal(150m, result.Aggregates[0].Open);
        // Should NOT have called the polygon service
        _polygonServiceMock.Verify(
            p => p.FetchAggregatesAsync(It.IsAny<string>(), It.IsAny<int>(),
                It.IsAny<string>(), It.IsAny<string>(), It.IsAny<string>(),
                It.IsAny<CancellationToken>()),
            Times.Never);
    }

    #endregion

    #region FetchAndStoreAggregatesAsync — Upsert Logic

    [Fact]
    public async Task FetchAndStoreAggregatesAsync_NewData_InsertsAggregates()
    {
        var context = TestDbContextFactory.Create();
        var service = new MarketDataService(context, _polygonServiceMock.Object, _loggerMock.Object);

        var aggregateResponse = new AggregateResponse
        {
            Success = true,
            Ticker = "AAPL",
            DataType = "aggregates",
            Data =
            [
                new AggregateData
                {
                    Timestamp = "2026-01-15T00:00:00Z",
                    Open = 150m, High = 155m, Low = 148m, Close = 153m,
                    Volume = 1_000_000m, Vwap = 151.5m, Transactions = 5000
                },
                new AggregateData
                {
                    Timestamp = "2026-01-16T00:00:00Z",
                    Open = 153m, High = 158m, Low = 151m, Close = 156m,
                    Volume = 1_200_000m, Vwap = 154m, Transactions = 6000
                }
            ],
            Summary = new DataSummary
            {
                OriginalCount = 2, CleanedCount = 2, RemovedCount = 0
            }
        };

        _polygonServiceMock
            .Setup(p => p.FetchAggregatesAsync("AAPL", 1, "day", "2026-01-15", "2026-01-16",
                It.IsAny<CancellationToken>()))
            .ReturnsAsync(aggregateResponse);

        var result = await service.FetchAndStoreAggregatesAsync(
            "AAPL", 1, "day", "2026-01-15", "2026-01-16");

        Assert.Equal(2, result.Count);
        Assert.Equal(150m, result[0].Open);
        Assert.Equal(153m, result[1].Open);

        // Verify persisted to DB
        var dbCount = context.StockAggregates.Count();
        Assert.Equal(2, dbCount);
    }

    [Fact]
    public async Task FetchAndStoreAggregatesAsync_ExistingData_UpdatesInPlace()
    {
        var context = TestDbContextFactory.Create();
        var service = new MarketDataService(context, _polygonServiceMock.Object, _loggerMock.Object);

        // Pre-seed ticker and an old aggregate
        var ticker = new Ticker { Symbol = "AAPL", Name = "AAPL", Market = "stocks" };
        context.Tickers.Add(ticker);
        await context.SaveChangesAsync();

        var existingAggregate = new StockAggregate
        {
            TickerId = ticker.Id,
            Open = 100m, High = 105m, Low = 98m, Close = 102m,
            Volume = 500_000m, Timespan = "day", Multiplier = 1,
            Timestamp = new DateTime(2026, 1, 15, 0, 0, 0, DateTimeKind.Utc)
        };
        context.StockAggregates.Add(existingAggregate);
        await context.SaveChangesAsync();

        // Now fetch "new" data for the same timestamp with updated prices
        var aggregateResponse = new AggregateResponse
        {
            Success = true,
            Ticker = "AAPL",
            DataType = "aggregates",
            Data =
            [
                new AggregateData
                {
                    Timestamp = "2026-01-15T00:00:00Z",
                    Open = 150m, High = 155m, Low = 148m, Close = 153m,
                    Volume = 1_000_000m, Vwap = 151.5m, Transactions = 5000
                }
            ],
            Summary = new DataSummary { OriginalCount = 1, CleanedCount = 1, RemovedCount = 0 }
        };

        _polygonServiceMock
            .Setup(p => p.FetchAggregatesAsync("AAPL", 1, "day", "2026-01-15", "2026-01-15",
                It.IsAny<CancellationToken>()))
            .ReturnsAsync(aggregateResponse);

        var result = await service.FetchAndStoreAggregatesAsync(
            "AAPL", 1, "day", "2026-01-15", "2026-01-15");

        // Should update, not duplicate
        Assert.Single(result);
        Assert.Equal(150m, result[0].Open); // updated value

        var dbCount = context.StockAggregates.Count();
        Assert.Equal(1, dbCount); // no duplicate
    }

    [Fact]
    public async Task FetchAndStoreAggregatesAsync_EmptyResponse_ReturnsEmptyList()
    {
        var context = TestDbContextFactory.Create();
        var service = new MarketDataService(context, _polygonServiceMock.Object, _loggerMock.Object);

        var emptyResponse = new AggregateResponse
        {
            Success = true,
            Ticker = "AAPL",
            DataType = "aggregates",
            Data = [],
            Summary = null
        };

        _polygonServiceMock
            .Setup(p => p.FetchAggregatesAsync("AAPL", 1, "day", "2026-01-15", "2026-01-16",
                It.IsAny<CancellationToken>()))
            .ReturnsAsync(emptyResponse);

        var result = await service.FetchAndStoreAggregatesAsync(
            "AAPL", 1, "day", "2026-01-15", "2026-01-16");

        Assert.Empty(result);
    }

    [Fact]
    public async Task FetchAndStoreAggregatesAsync_OptionsPrefix_DetectsOptionsMarket()
    {
        var context = TestDbContextFactory.Create();
        var service = new MarketDataService(context, _polygonServiceMock.Object, _loggerMock.Object);

        var aggregateResponse = new AggregateResponse
        {
            Success = true,
            Ticker = "O:AAPL260117C00150000",
            DataType = "aggregates",
            Data =
            [
                new AggregateData
                {
                    Timestamp = "2026-01-15T00:00:00Z",
                    Open = 5m, High = 6m, Low = 4m, Close = 5.5m,
                    Volume = 100m
                }
            ],
            Summary = new DataSummary { OriginalCount = 1, CleanedCount = 1, RemovedCount = 0 }
        };

        _polygonServiceMock
            .Setup(p => p.FetchAggregatesAsync(
                "O:AAPL260117C00150000", It.IsAny<int>(), It.IsAny<string>(),
                It.IsAny<string>(), It.IsAny<string>(), It.IsAny<CancellationToken>()))
            .ReturnsAsync(aggregateResponse);

        await service.FetchAndStoreAggregatesAsync(
            "O:AAPL260117C00150000", 1, "day", "2026-01-15", "2026-01-15");

        // Should have created a ticker with "options" market
        var tickerEntity = context.Tickers.FirstOrDefault(t => t.Symbol == "O:AAPL260117C00150000");
        Assert.NotNull(tickerEntity);
        Assert.Equal("options", tickerEntity!.Market);
    }

    [Fact]
    public async Task FetchAndStoreAggregatesAsync_MixedNewAndExisting_UpsertsBothCorrectly()
    {
        var context = TestDbContextFactory.Create();
        var service = new MarketDataService(context, _polygonServiceMock.Object, _loggerMock.Object);

        // Pre-seed ticker and one existing aggregate
        var ticker = new Ticker { Symbol = "MSFT", Name = "MSFT", Market = "stocks" };
        context.Tickers.Add(ticker);
        await context.SaveChangesAsync();

        context.StockAggregates.Add(new StockAggregate
        {
            TickerId = ticker.Id,
            Open = 300m, High = 305m, Low = 298m, Close = 303m,
            Volume = 800_000m, Timespan = "day", Multiplier = 1,
            Timestamp = new DateTime(2026, 1, 15, 0, 0, 0, DateTimeKind.Utc)
        });
        await context.SaveChangesAsync();

        // Response has the existing date (updated) plus a new date
        var response = new AggregateResponse
        {
            Success = true,
            Ticker = "MSFT",
            DataType = "aggregates",
            Data =
            [
                new AggregateData
                {
                    Timestamp = "2026-01-15T00:00:00Z",
                    Open = 310m, High = 315m, Low = 308m, Close = 313m,
                    Volume = 900_000m
                },
                new AggregateData
                {
                    Timestamp = "2026-01-16T00:00:00Z",
                    Open = 313m, High = 320m, Low = 310m, Close = 318m,
                    Volume = 1_100_000m
                }
            ],
            Summary = new DataSummary { OriginalCount = 2, CleanedCount = 2, RemovedCount = 0 }
        };

        _polygonServiceMock
            .Setup(p => p.FetchAggregatesAsync("MSFT", 1, "day", "2026-01-15", "2026-01-16",
                It.IsAny<CancellationToken>()))
            .ReturnsAsync(response);

        var result = await service.FetchAndStoreAggregatesAsync(
            "MSFT", 1, "day", "2026-01-15", "2026-01-16");

        Assert.Equal(2, result.Count);

        // Updated record
        Assert.Equal(310m, result[0].Open);

        // New record
        Assert.Equal(313m, result[1].Open);

        // Total in DB should be 2 (not 3)
        var dbCount = context.StockAggregates.Count();
        Assert.Equal(2, dbCount);
    }

    #endregion

    #region GetOrFetchAggregatesAsync — Force Refresh

    [Fact]
    public async Task GetOrFetchAggregatesAsync_ForceRefresh_BypassesCache()
    {
        var context = TestDbContextFactory.Create();
        var service = new MarketDataService(context, _polygonServiceMock.Object, _loggerMock.Object);

        // Seed cached data
        var ticker = new Ticker { Symbol = "GOOG", Name = "GOOG", Market = "stocks" };
        context.Tickers.Add(ticker);
        await context.SaveChangesAsync();

        context.StockAggregates.Add(new StockAggregate
        {
            TickerId = ticker.Id,
            Open = 100m, High = 105m, Low = 98m, Close = 102m,
            Volume = 500_000m, Timespan = "day", Multiplier = 1,
            Timestamp = new DateTime(2026, 1, 15, 0, 0, 0, DateTimeKind.Utc)
        });
        await context.SaveChangesAsync();

        // Mock polygon to return fresh data
        var freshResponse = new AggregateResponse
        {
            Success = true,
            Ticker = "GOOG",
            DataType = "aggregates",
            Data =
            [
                new AggregateData
                {
                    Timestamp = "2026-01-15T00:00:00Z",
                    Open = 200m, High = 205m, Low = 198m, Close = 203m,
                    Volume = 700_000m
                }
            ],
            Summary = new DataSummary { OriginalCount = 1, CleanedCount = 1, RemovedCount = 0 }
        };

        _polygonServiceMock
            .Setup(p => p.FetchAggregatesAsync("GOOG", 1, "day", "2026-01-01", "2026-01-31",
                It.IsAny<CancellationToken>()))
            .ReturnsAsync(freshResponse);

        var result = await service.GetOrFetchAggregatesAsync(
            "GOOG", 1, "day", "2026-01-01", "2026-01-31", forceRefresh: true);

        // Should have called polygon despite cached data
        _polygonServiceMock.Verify(
            p => p.FetchAggregatesAsync("GOOG", 1, "day", "2026-01-01", "2026-01-31",
                It.IsAny<CancellationToken>()),
            Times.Once);

        Assert.Single(result.Aggregates);
        Assert.Equal(200m, result.Aggregates[0].Open); // fresh data
    }

    #endregion

    #region GetOrFetchAggregatesAsync — Cache Miss

    [Fact]
    public async Task GetOrFetchAggregatesAsync_CacheMiss_FetchesFromPolygon()
    {
        var context = TestDbContextFactory.Create();
        var service = new MarketDataService(context, _polygonServiceMock.Object, _loggerMock.Object);

        // No seeded data — cache miss
        var response = new AggregateResponse
        {
            Success = true,
            Ticker = "NVDA",
            DataType = "aggregates",
            Data =
            [
                new AggregateData
                {
                    Timestamp = "2026-01-15T00:00:00Z",
                    Open = 800m, High = 810m, Low = 790m, Close = 805m,
                    Volume = 2_000_000m
                }
            ],
            Summary = new DataSummary { OriginalCount = 1, CleanedCount = 1, RemovedCount = 0 }
        };

        _polygonServiceMock
            .Setup(p => p.FetchAggregatesAsync("NVDA", 1, "day", "2026-01-15", "2026-01-15",
                It.IsAny<CancellationToken>()))
            .ReturnsAsync(response);

        var result = await service.GetOrFetchAggregatesAsync(
            "NVDA", 1, "day", "2026-01-15", "2026-01-15");

        Assert.Single(result.Aggregates);
        Assert.Equal(800m, result.Aggregates[0].Open);

        _polygonServiceMock.Verify(
            p => p.FetchAggregatesAsync("NVDA", 1, "day", "2026-01-15", "2026-01-15",
                It.IsAny<CancellationToken>()),
            Times.Once);
    }

    #endregion

    #region FetchAndStoreAggregatesAsync — Edge Cases

    [Fact]
    public async Task FetchAndStoreAggregatesAsync_NullDataList_ReturnsEmpty()
    {
        var context = TestDbContextFactory.Create();
        var service = new MarketDataService(context, _polygonServiceMock.Object, _loggerMock.Object);

        var nullDataResponse = new AggregateResponse
        {
            Success = true,
            Ticker = "AAPL",
            DataType = "aggregates",
            Data = null,
            Summary = null
        };

        _polygonServiceMock
            .Setup(p => p.FetchAggregatesAsync("AAPL", 1, "day", "2026-01-15", "2026-01-16",
                It.IsAny<CancellationToken>()))
            .ReturnsAsync(nullDataResponse);

        var result = await service.FetchAndStoreAggregatesAsync(
            "AAPL", 1, "day", "2026-01-15", "2026-01-16");

        Assert.Empty(result);
    }

    [Fact]
    public async Task GetOrFetchAggregatesAsync_CancellationRequested_ThrowsOperationCanceled()
    {
        var context = TestDbContextFactory.Create();
        var service = new MarketDataService(context, _polygonServiceMock.Object, _loggerMock.Object);

        using var cts = new CancellationTokenSource();
        cts.Cancel();

        await Assert.ThrowsAnyAsync<OperationCanceledException>(() =>
            service.GetOrFetchAggregatesAsync(
                "AAPL", 1, "day", "2026-01-01", "2026-01-31",
                cancellationToken: cts.Token));
    }

    [Fact]
    public async Task FetchAndStoreAggregatesAsync_PolygonServiceThrows_RethrowsException()
    {
        var context = TestDbContextFactory.Create();
        var service = new MarketDataService(context, _polygonServiceMock.Object, _loggerMock.Object);

        _polygonServiceMock
            .Setup(p => p.FetchAggregatesAsync("AAPL", 1, "day", "2026-01-15", "2026-01-16",
                It.IsAny<CancellationToken>()))
            .ThrowsAsync(new HttpRequestException("Service unavailable"));

        await Assert.ThrowsAsync<HttpRequestException>(() =>
            service.FetchAndStoreAggregatesAsync(
                "AAPL", 1, "day", "2026-01-15", "2026-01-16"));
    }

    #endregion
}

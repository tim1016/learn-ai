using Backend.GraphQL;
using Backend.Models.DTOs;
using Backend.Models.DTOs.PolygonResponses;
using Backend.Models.MarketData;
using Backend.Services.Interfaces;
using Backend.Tests.Helpers;
using Microsoft.Extensions.Logging;
using Moq;

namespace Backend.Tests.Unit.GraphQL;

public class QueryTests
{
    private readonly Mock<IMarketDataService> _marketDataMock = new();
    private readonly Mock<ITechnicalAnalysisService> _taMock = new();
    private readonly Mock<IPolygonService> _polygonMock = new();
    private readonly Mock<ILogger<Query>> _loggerMock = new();

    #region GetOrFetchStockAggregates

    [Fact]
    public async Task GetOrFetchStockAggregates_WithData_ReturnsSummary()
    {
        var context = TestDbContextFactory.Create();
        var ticker = new Ticker { Symbol = "AAPL", Name = "AAPL", Market = "stocks" };
        context.Tickers.Add(ticker);
        await context.SaveChangesAsync();

        var aggregates = new List<StockAggregate>
        {
            new() { TickerId = ticker.Id, Open = 150m, High = 160m, Low = 145m, Close = 155m, Volume = 1_000_000m, Timestamp = new DateTime(2026, 1, 15, 0, 0, 0, DateTimeKind.Utc), Timespan = "day", Multiplier = 1 },
            new() { TickerId = ticker.Id, Open = 155m, High = 165m, Low = 150m, Close = 162m, Volume = 900_000m, Timestamp = new DateTime(2026, 1, 16, 0, 0, 0, DateTimeKind.Utc), Timespan = "day", Multiplier = 1 },
        };

        _marketDataMock.Setup(s => s.GetOrFetchAggregatesAsync(
                "AAPL", 1, "day", "2026-01-01", "2026-01-31", false, default))
            .ReturnsAsync(aggregates);

        var query = new Query();
        var result = await query.GetOrFetchStockAggregates(
            _marketDataMock.Object, _loggerMock.Object, context,
            "AAPL", "2026-01-01", "2026-01-31");

        Assert.Equal("AAPL", result.Ticker);
        Assert.Equal(2, result.Aggregates.Count);
        Assert.NotNull(result.Summary);
        Assert.Equal(165m, result.Summary!.PeriodHigh);
        Assert.Equal(145m, result.Summary.PeriodLow);
        Assert.Equal(2, result.Summary.TotalBars);
    }

    [Fact]
    public async Task GetOrFetchStockAggregates_NoData_ReturnsNullSummary()
    {
        var context = TestDbContextFactory.Create();

        _marketDataMock.Setup(s => s.GetOrFetchAggregatesAsync(
                It.IsAny<string>(), It.IsAny<int>(), It.IsAny<string>(),
                It.IsAny<string>(), It.IsAny<string>(), false, default))
            .ReturnsAsync(new List<StockAggregate>());

        var query = new Query();
        var result = await query.GetOrFetchStockAggregates(
            _marketDataMock.Object, _loggerMock.Object, context,
            "AAPL", "2026-01-01", "2026-01-31");

        Assert.Equal("AAPL", result.Ticker);
        Assert.Empty(result.Aggregates);
        Assert.Null(result.Summary);
    }

    [Fact]
    public async Task GetOrFetchStockAggregates_ComputesPriceChange()
    {
        var context = TestDbContextFactory.Create();

        var aggregates = new List<StockAggregate>
        {
            new() { Open = 100m, High = 110m, Low = 95m, Close = 105m, Volume = 500_000m, Timestamp = DateTime.UtcNow.AddDays(-1), Timespan = "day", Multiplier = 1 },
            new() { Open = 105m, High = 115m, Low = 100m, Close = 112m, Volume = 600_000m, Timestamp = DateTime.UtcNow, Timespan = "day", Multiplier = 1 },
        };

        _marketDataMock.Setup(s => s.GetOrFetchAggregatesAsync(
                It.IsAny<string>(), It.IsAny<int>(), It.IsAny<string>(),
                It.IsAny<string>(), It.IsAny<string>(), false, default))
            .ReturnsAsync(aggregates);

        var query = new Query();
        var result = await query.GetOrFetchStockAggregates(
            _marketDataMock.Object, _loggerMock.Object, context,
            "AAPL", "2026-01-01", "2026-01-31");

        // PriceChange = last close - first open = 112 - 100 = 12
        Assert.Equal(12m, result.Summary!.PriceChange);
        Assert.Equal(12m, result.Summary.PriceChangePercent); // (12/100)*100 = 12%
    }

    #endregion

    #region CheckCachedRanges

    [Fact]
    public async Task CheckCachedRanges_NoTicker_ReturnsAllUncached()
    {
        var context = TestDbContextFactory.Create();

        var query = new Query();
        var ranges = new List<DateRangeInput>
        {
            new() { FromDate = "2026-01-01", ToDate = "2026-01-31" },
            new() { FromDate = "2026-02-01", ToDate = "2026-02-28" },
        };

        var result = await query.CheckCachedRanges(context, "AAPL", ranges);

        Assert.Equal(2, result.Count);
        Assert.All(result, r => Assert.False(r.IsCached));
    }

    [Fact]
    public async Task CheckCachedRanges_WithCachedData_ReturnsCachedTrue()
    {
        var context = TestDbContextFactory.Create();

        var ticker = new Ticker { Symbol = "AAPL", Name = "AAPL", Market = "stocks" };
        context.Tickers.Add(ticker);
        await context.SaveChangesAsync();

        context.StockAggregates.Add(new StockAggregate
        {
            TickerId = ticker.Id,
            Open = 150m, High = 155m, Low = 148m, Close = 153m, Volume = 1_000_000m,
            Timestamp = new DateTime(2026, 1, 15, 0, 0, 0, DateTimeKind.Utc),
            Timespan = "day", Multiplier = 1,
        });
        await context.SaveChangesAsync();

        var query = new Query();
        var ranges = new List<DateRangeInput>
        {
            new() { FromDate = "2026-01-01", ToDate = "2026-01-31" },
            new() { FromDate = "2026-03-01", ToDate = "2026-03-31" },
        };

        var result = await query.CheckCachedRanges(context, "AAPL", ranges);

        Assert.True(result[0].IsCached);
        Assert.False(result[1].IsCached);
    }

    #endregion

    #region GetOptionsContracts

    [Fact]
    public async Task GetOptionsContracts_Success_MapsContracts()
    {
        _polygonMock.Setup(s => s.FetchOptionsContractsAsync(
                "AAPL", null, null, null, null, null, null, null, 100, default))
            .ReturnsAsync(new OptionsContractsResponse
            {
                Success = true,
                Contracts = [new OptionsContractDto
                {
                    Ticker = "O:AAPL260220C00230000",
                    UnderlyingTicker = "AAPL",
                    ContractType = "call",
                    StrikePrice = 230m,
                    ExpirationDate = "2026-02-20",
                }],
                Count = 1
            });

        var query = new Query();
        var result = await query.GetOptionsContracts(
            _polygonMock.Object, _loggerMock.Object, "AAPL");

        Assert.True(result.Success);
        Assert.Single(result.Contracts);
        Assert.Equal("call", result.Contracts[0].ContractType);
        Assert.Equal(230m, result.Contracts[0].StrikePrice);
    }

    [Fact]
    public async Task GetOptionsContracts_ServiceThrows_ReturnsErrorResult()
    {
        _polygonMock.Setup(s => s.FetchOptionsContractsAsync(
                It.IsAny<string>(), It.IsAny<string?>(), It.IsAny<string?>(),
                It.IsAny<decimal?>(), It.IsAny<decimal?>(), It.IsAny<string?>(),
                It.IsAny<string?>(), It.IsAny<string?>(), It.IsAny<int>(),
                It.IsAny<CancellationToken>()))
            .ThrowsAsync(new HttpRequestException("Connection refused"));

        var query = new Query();
        var result = await query.GetOptionsContracts(
            _polygonMock.Object, _loggerMock.Object, "AAPL");

        Assert.False(result.Success);
        Assert.Contains("Connection refused", result.Error);
    }

    #endregion

    #region GetOptionsChainSnapshot

    [Fact]
    public async Task GetOptionsChainSnapshot_Success_MapsFullResponse()
    {
        _polygonMock.Setup(s => s.FetchOptionsChainSnapshotAsync("AAPL", "2026-02-20", default))
            .ReturnsAsync(new OptionsChainSnapshotResponse
            {
                Success = true,
                Underlying = new UnderlyingSnapshotDto { Ticker = "AAPL", Price = 230m, Change = 2.5m, ChangePercent = 1.1m },
                Contracts = [new OptionsContractSnapshotDto
                {
                    Ticker = "O:AAPL260220C00230000",
                    ContractType = "call",
                    StrikePrice = 230m,
                    ImpliedVolatility = 0.35m,
                    Greeks = new GreeksSnapshotDto { Delta = 0.5m, Gamma = 0.02m, Theta = -0.05m, Vega = 0.15m },
                    Day = new DaySnapshotDto { Open = 5m, High = 6m, Low = 4.5m, Close = 5.5m, Volume = 500m }
                }],
                Count = 1
            });

        var query = new Query();
        var result = await query.GetOptionsChainSnapshot(
            _polygonMock.Object, _loggerMock.Object, "AAPL", "2026-02-20");

        Assert.True(result.Success);
        Assert.NotNull(result.Underlying);
        Assert.Equal(230m, result.Underlying!.Price);
        Assert.Single(result.Contracts);
        Assert.Equal(0.5m, result.Contracts[0].Greeks!.Delta);
        Assert.Equal(5m, result.Contracts[0].Day!.Open);
    }

    [Fact]
    public async Task GetOptionsChainSnapshot_NullUnderlying_HandledGracefully()
    {
        _polygonMock.Setup(s => s.FetchOptionsChainSnapshotAsync("AAPL", null, default))
            .ReturnsAsync(new OptionsChainSnapshotResponse
            {
                Success = true,
                Underlying = null,
                Contracts = [],
                Count = 0
            });

        var query = new Query();
        var result = await query.GetOptionsChainSnapshot(
            _polygonMock.Object, _loggerMock.Object, "AAPL");

        Assert.True(result.Success);
        Assert.Null(result.Underlying);
        Assert.Empty(result.Contracts);
    }

    [Fact]
    public async Task GetOptionsChainSnapshot_ServiceThrows_ReturnsErrorResult()
    {
        _polygonMock.Setup(s => s.FetchOptionsChainSnapshotAsync(
                It.IsAny<string>(), It.IsAny<string?>(), It.IsAny<CancellationToken>()))
            .ThrowsAsync(new HttpRequestException("Timeout"));

        var query = new Query();
        var result = await query.GetOptionsChainSnapshot(
            _polygonMock.Object, _loggerMock.Object, "AAPL");

        Assert.False(result.Success);
        Assert.Contains("Timeout", result.Error);
    }

    #endregion

    #region CalculateIndicators

    [Fact]
    public async Task CalculateIndicators_NoTicker_ReturnsFailure()
    {
        var context = TestDbContextFactory.Create();

        var query = new Query();
        var indicators = new List<IndicatorConfigInput> { new() { Name = "sma", Window = 20 } };

        var result = await query.CalculateIndicators(
            _taMock.Object, _loggerMock.Object, context,
            "AAPL", "2026-01-01", "2026-01-31", indicators);

        Assert.False(result.Success);
        Assert.Contains("No data found", result.Message);
    }

    [Fact]
    public async Task CalculateIndicators_NoAggregates_ReturnsFailure()
    {
        var context = TestDbContextFactory.Create();
        var ticker = new Ticker { Symbol = "AAPL", Name = "AAPL", Market = "stocks" };
        context.Tickers.Add(ticker);
        await context.SaveChangesAsync();

        var query = new Query();
        var indicators = new List<IndicatorConfigInput> { new() { Name = "sma", Window = 20 } };

        var result = await query.CalculateIndicators(
            _taMock.Object, _loggerMock.Object, context,
            "AAPL", "2026-01-01", "2026-01-31", indicators);

        Assert.False(result.Success);
        Assert.Contains("No aggregate data", result.Message);
    }

    [Fact]
    public async Task CalculateIndicators_WithData_ReturnsIndicators()
    {
        var context = TestDbContextFactory.Create();
        var ticker = new Ticker { Symbol = "AAPL", Name = "AAPL", Market = "stocks" };
        context.Tickers.Add(ticker);
        await context.SaveChangesAsync();

        context.StockAggregates.Add(new StockAggregate
        {
            TickerId = ticker.Id, Open = 150m, High = 155m, Low = 148m, Close = 153m,
            Volume = 1_000_000m, Timestamp = new DateTime(2026, 1, 15, 0, 0, 0, DateTimeKind.Utc),
            Timespan = "day", Multiplier = 1,
        });
        await context.SaveChangesAsync();

        _taMock.Setup(s => s.CalculateIndicatorsAsync(
                "AAPL", It.IsAny<List<OhlcvBarDto>>(), It.IsAny<List<IndicatorConfigDto>>(), default))
            .ReturnsAsync(new CalculateIndicatorsResponseDto(
                Success: true, Ticker: "AAPL",
                Indicators: [new IndicatorResultDto("sma", 20, [
                    new IndicatorDataPointDto(1705276800000, 152.5m, null, null, null, null)
                ])],
                Error: null
            ));

        var query = new Query();
        var indicators = new List<IndicatorConfigInput> { new() { Name = "sma", Window = 20 } };

        var result = await query.CalculateIndicators(
            _taMock.Object, _loggerMock.Object, context,
            "AAPL", "2026-01-01", "2026-01-31", indicators);

        Assert.True(result.Success);
        Assert.Single(result.Indicators);
        Assert.Equal("sma", result.Indicators[0].Name);
    }

    [Fact]
    public async Task CalculateIndicators_ServiceThrows_ReturnsErrorResult()
    {
        var context = TestDbContextFactory.Create();
        var ticker = new Ticker { Symbol = "AAPL", Name = "AAPL", Market = "stocks" };
        context.Tickers.Add(ticker);
        await context.SaveChangesAsync();

        context.StockAggregates.Add(new StockAggregate
        {
            TickerId = ticker.Id, Open = 150m, High = 155m, Low = 148m, Close = 153m,
            Volume = 1_000_000m, Timestamp = new DateTime(2026, 1, 15, 0, 0, 0, DateTimeKind.Utc),
            Timespan = "day", Multiplier = 1,
        });
        await context.SaveChangesAsync();

        _taMock.Setup(s => s.CalculateIndicatorsAsync(
                It.IsAny<string>(), It.IsAny<List<OhlcvBarDto>>(),
                It.IsAny<List<IndicatorConfigDto>>(), It.IsAny<CancellationToken>()))
            .ThrowsAsync(new HttpRequestException("Python service down"));

        var query = new Query();
        var indicators = new List<IndicatorConfigInput> { new() { Name = "sma", Window = 20 } };

        var result = await query.CalculateIndicators(
            _taMock.Object, _loggerMock.Object, context,
            "AAPL", "2026-01-01", "2026-01-31", indicators);

        Assert.False(result.Success);
        Assert.Contains("Python service down", result.Message);
    }

    #endregion
}

using Backend.GraphQL;
using Backend.Models.DTOs;
using Backend.Models.MarketData;
using Backend.Services.Interfaces;
using Backend.Tests.Helpers;
using Microsoft.Extensions.Logging;
using Moq;

namespace Backend.Tests.Unit.GraphQL;

public class MutationSanitizeAndBacktestTests
{
    private readonly Mock<ISanitizationService> _sanitizationMock = new();
    private readonly Mock<IBacktestService> _backtestMock = new();
    private readonly Mock<IMarketDataService> _marketDataMock = new();
    private readonly Mock<ILogger<Mutation>> _loggerMock = new();

    #region SanitizeMarketData

    [Fact]
    public async Task SanitizeMarketData_Success_ReturnsCounts()
    {
        var input = new List<MarketDataRecord>
        {
            new("AAPL", 150m, 155m, 148m, 153m, 1_000_000m, 1704067200000),
            new("AAPL", 153m, 158m, 151m, 157m, 900_000m, 1704153600000),
        };

        _sanitizationMock.Setup(s => s.SanitizeAsync(input, 0.99, default))
            .ReturnsAsync([input[0]]); // Simulates one record removed

        var mutation = new Mutation();
        var result = await mutation.SanitizeMarketData(_sanitizationMock.Object, input);

        Assert.True(result.Success);
        Assert.Equal(2, result.OriginalCount);
        Assert.Equal(1, result.CleanedCount);
        Assert.Contains("2 records", result.Message);
    }

    [Fact]
    public async Task SanitizeMarketData_ServiceThrows_ReturnsErrorResult()
    {
        var input = new List<MarketDataRecord>
        {
            new("AAPL", 150m, 155m, 148m, 153m, 1_000_000m, 1704067200000),
        };

        _sanitizationMock.Setup(s => s.SanitizeAsync(
                It.IsAny<List<MarketDataRecord>>(), It.IsAny<double>(), It.IsAny<CancellationToken>()))
            .ThrowsAsync(new HttpRequestException("Service unavailable"));

        var mutation = new Mutation();
        var result = await mutation.SanitizeMarketData(_sanitizationMock.Object, input);

        Assert.False(result.Success);
        Assert.Equal(0, result.CleanedCount);
        Assert.Contains("Service unavailable", result.Message);
    }

    #endregion

    #region RunBacktest

    [Fact]
    public async Task RunBacktest_NoAggregates_ReturnsFailure()
    {
        var context = TestDbContextFactory.Create();

        _marketDataMock.Setup(s => s.GetOrFetchAggregatesAsync(
                It.IsAny<string>(), It.IsAny<int>(), It.IsAny<string>(),
                It.IsAny<string>(), It.IsAny<string>(), false, default))
            .ReturnsAsync(new List<StockAggregate>());

        var mutation = new Mutation();
        var result = await mutation.RunBacktest(
            _backtestMock.Object, _marketDataMock.Object, _loggerMock.Object, context,
            "AAPL", "sma_crossover", "2026-01-01", "2026-01-31");

        Assert.False(result.Success);
        Assert.Contains("No aggregates found", result.Error);
    }

    [Fact]
    public async Task RunBacktest_NoTickerInDb_ReturnsFailure()
    {
        var context = TestDbContextFactory.Create();

        var aggregates = new List<StockAggregate>
        {
            new() { Open = 150m, High = 155m, Low = 148m, Close = 153m, Volume = 1_000_000m, Timespan = "day" }
        };

        _marketDataMock.Setup(s => s.GetOrFetchAggregatesAsync(
                It.IsAny<string>(), It.IsAny<int>(), It.IsAny<string>(),
                It.IsAny<string>(), It.IsAny<string>(), false, default))
            .ReturnsAsync(aggregates);

        var mutation = new Mutation();
        var result = await mutation.RunBacktest(
            _backtestMock.Object, _marketDataMock.Object, _loggerMock.Object, context,
            "AAPL", "sma_crossover", "2026-01-01", "2026-01-31");

        Assert.False(result.Success);
        Assert.Contains("not found", result.Error);
    }

    [Fact]
    public async Task RunBacktest_Success_MapsExecutionToResult()
    {
        var context = TestDbContextFactory.Create();
        var ticker = new Ticker { Symbol = "AAPL", Name = "AAPL", Market = "stocks" };
        context.Tickers.Add(ticker);
        await context.SaveChangesAsync();

        var aggregates = new List<StockAggregate>
        {
            new() { TickerId = ticker.Id, Open = 150m, High = 155m, Low = 148m, Close = 153m, Volume = 1_000_000m, Timespan = "minute" }
        };

        _marketDataMock.Setup(s => s.GetOrFetchAggregatesAsync(
                "AAPL", 1, "minute", "2026-01-01", "2026-01-31", false, default))
            .ReturnsAsync(aggregates);

        var execution = new StrategyExecution
        {
            Id = 42,
            TickerId = ticker.Id,
            StrategyName = "sma_crossover",
            Parameters = """{"ShortWindow":10}""",
            StartDate = "2026-01-01",
            EndDate = "2026-01-31",
            TotalTrades = 3,
            WinningTrades = 2,
            LosingTrades = 1,
            TotalPnL = 15.5m,
            MaxDrawdown = 5m,
            SharpeRatio = 1.2m,
            DurationMs = 50,
            Trades = [new BacktestTrade
            {
                TradeType = "Buy",
                EntryTimestamp = new DateTime(2026, 1, 10, 0, 0, 0, DateTimeKind.Utc),
                ExitTimestamp = new DateTime(2026, 1, 20, 0, 0, 0, DateTimeKind.Utc),
                EntryPrice = 150m,
                ExitPrice = 155m,
                PnL = 5m,
                CumulativePnL = 5m,
                SignalReason = "SMA(10) crossed below SMA(30)",
            }],
        };

        _backtestMock.Setup(s => s.RunBacktestAsync(
                ticker.Id, "sma_crossover", "{}", "2026-01-01", "2026-01-31",
                "minute", 1, aggregates, default))
            .ReturnsAsync(execution);

        var mutation = new Mutation();
        var result = await mutation.RunBacktest(
            _backtestMock.Object, _marketDataMock.Object, _loggerMock.Object, context,
            "AAPL", "sma_crossover", "2026-01-01", "2026-01-31");

        Assert.True(result.Success);
        Assert.Equal(42, result.Id);
        Assert.Equal(3, result.TotalTrades);
        Assert.Equal(15.5m, result.TotalPnL);
        Assert.Single(result.Trades);
        Assert.Equal("Buy", result.Trades[0].TradeType);
    }

    [Fact]
    public async Task RunBacktest_ServiceThrows_ReturnsErrorResult()
    {
        var context = TestDbContextFactory.Create();
        var ticker = new Ticker { Symbol = "AAPL", Name = "AAPL", Market = "stocks" };
        context.Tickers.Add(ticker);
        await context.SaveChangesAsync();

        var aggregates = new List<StockAggregate>
        {
            new() { TickerId = ticker.Id, Open = 150m, Timespan = "minute", High = 155m, Low = 148m, Close = 153m, Volume = 1_000_000m }
        };

        _marketDataMock.Setup(s => s.GetOrFetchAggregatesAsync(
                It.IsAny<string>(), It.IsAny<int>(), It.IsAny<string>(),
                It.IsAny<string>(), It.IsAny<string>(), false, default))
            .ReturnsAsync(aggregates);

        _backtestMock.Setup(s => s.RunBacktestAsync(
                It.IsAny<int>(), It.IsAny<string>(), It.IsAny<string>(),
                It.IsAny<string>(), It.IsAny<string>(), It.IsAny<string>(),
                It.IsAny<int>(), It.IsAny<List<StockAggregate>>(), It.IsAny<CancellationToken>()))
            .ThrowsAsync(new InvalidOperationException("Need at least 2 bars"));

        var mutation = new Mutation();
        var result = await mutation.RunBacktest(
            _backtestMock.Object, _marketDataMock.Object, _loggerMock.Object, context,
            "AAPL", "sma_crossover", "2026-01-01", "2026-01-31");

        Assert.False(result.Success);
        Assert.Contains("Need at least 2 bars", result.Error);
    }

    #endregion
}

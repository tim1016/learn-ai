using Backend.Models.MarketData;
using Backend.Services.Implementation;
using Backend.Tests.Helpers;
using Microsoft.Extensions.Logging;
using Moq;

namespace Backend.Tests.Unit.Services;

public class BacktestServiceTests
{
    private readonly Mock<ILogger<BacktestService>> _loggerMock = new();

    private BacktestService CreateService()
    {
        var context = TestDbContextFactory.Create();
        return new BacktestService(context, _loggerMock.Object);
    }

    private static List<StockAggregate> CreateBars(decimal[] closes, DateTime startDate)
    {
        return closes.Select((close, i) => new StockAggregate
        {
            TickerId = 1,
            Open = close - 1,
            High = close + 2,
            Low = close - 2,
            Close = close,
            Volume = 1_000_000,
            Timestamp = startDate.AddDays(i),
            Timespan = "day",
            Multiplier = 1,
        }).ToList();
    }

    #region RunBacktestAsync

    [Fact]
    public async Task RunBacktestAsync_LessThanTwoBars_ThrowsInvalidOperation()
    {
        var service = CreateService();
        var bars = CreateBars([100m], new DateTime(2026, 1, 1, 0, 0, 0, DateTimeKind.Utc));

        await Assert.ThrowsAsync<InvalidOperationException>(() =>
            service.RunBacktestAsync(1, "sma_crossover", "{}", "2026-01-01", "2026-01-31", "day", 1, bars));
    }

    [Fact]
    public async Task RunBacktestAsync_UnknownStrategy_ThrowsArgumentException()
    {
        var service = CreateService();
        var bars = CreateBars([100m, 101m, 102m], new DateTime(2026, 1, 1, 0, 0, 0, DateTimeKind.Utc));

        await Assert.ThrowsAsync<ArgumentException>(() =>
            service.RunBacktestAsync(1, "unknown_strategy", "{}", "2026-01-01", "2026-01-31", "day", 1, bars));
    }

    [Fact]
    public async Task RunBacktestAsync_SmaCrossover_ReturnsExecution()
    {
        var service = CreateService();

        // Create enough bars for SMA crossover (need at least longWindow = 30)
        var closes = Enumerable.Range(0, 50)
            .Select(i => 100m + i * 0.5m)
            .ToArray();
        var bars = CreateBars(closes, new DateTime(2026, 1, 1, 0, 0, 0, DateTimeKind.Utc));

        var result = await service.RunBacktestAsync(
            1, "sma_crossover", """{"ShortWindow":5,"LongWindow":10}""",
            "2026-01-01", "2026-02-19", "day", 1, bars);

        Assert.NotNull(result);
        Assert.Equal("sma_crossover", result.StrategyName);
        Assert.Equal(1, result.TickerId);
        Assert.True(result.DurationMs >= 0);
    }

    [Fact]
    public async Task RunBacktestAsync_SmaCrossover_TooFewBarsForWindow_ReturnsZeroTrades()
    {
        var service = CreateService();
        var bars = CreateBars([100m, 101m, 102m, 103m, 104m],
            new DateTime(2026, 1, 1, 0, 0, 0, DateTimeKind.Utc));

        var result = await service.RunBacktestAsync(
            1, "sma_crossover", """{"ShortWindow":10,"LongWindow":30}""",
            "2026-01-01", "2026-01-05", "day", 1, bars);

        Assert.Equal(0, result.TotalTrades);
        Assert.Equal(0m, result.TotalPnL);
    }

    [Fact]
    public async Task RunBacktestAsync_RsiMeanReversion_ReturnsExecution()
    {
        var service = CreateService();

        // Create bars that will trigger RSI signals:
        // Start high, dip sharply (oversold), then recover sharply (overbought)
        var closes = new List<decimal>();
        // Initial period: stable at 100
        for (int i = 0; i < 20; i++) closes.Add(100m);
        // Sharp decline to trigger oversold
        for (int i = 0; i < 10; i++) closes.Add(100m - i * 3m);
        // Sharp recovery to trigger overbought
        for (int i = 0; i < 15; i++) closes.Add(70m + i * 5m);

        var bars = CreateBars(closes.ToArray(), new DateTime(2026, 1, 1, 0, 0, 0, DateTimeKind.Utc));

        var result = await service.RunBacktestAsync(
            1, "rsi_mean_reversion", """{"Window":14,"Oversold":30,"Overbought":70}""",
            "2026-01-01", "2026-02-14", "day", 1, bars);

        Assert.NotNull(result);
        Assert.Equal("rsi_mean_reversion", result.StrategyName);
    }

    [Fact]
    public async Task RunBacktestAsync_RsiMeanReversion_TooFewBars_ReturnsZeroTrades()
    {
        var service = CreateService();
        var bars = CreateBars([100m, 101m, 102m],
            new DateTime(2026, 1, 1, 0, 0, 0, DateTimeKind.Utc));

        var result = await service.RunBacktestAsync(
            1, "rsi_mean_reversion", """{"Window":14}""",
            "2026-01-01", "2026-01-03", "day", 1, bars);

        Assert.Equal(0, result.TotalTrades);
    }

    [Fact]
    public async Task RunBacktestAsync_PersistsToDatabase()
    {
        var context = TestDbContextFactory.Create();
        var service = new BacktestService(context, _loggerMock.Object);

        // Seed a ticker
        var ticker = new Ticker { Symbol = "AAPL", Name = "AAPL", Market = "stocks" };
        context.Tickers.Add(ticker);
        await context.SaveChangesAsync();

        var closes = Enumerable.Range(0, 50)
            .Select(i => 100m + i * 0.5m)
            .ToArray();
        var bars = CreateBars(closes, new DateTime(2026, 1, 1, 0, 0, 0, DateTimeKind.Utc));
        foreach (var bar in bars) bar.TickerId = ticker.Id;

        var result = await service.RunBacktestAsync(
            ticker.Id, "sma_crossover", """{"ShortWindow":5,"LongWindow":10}""",
            "2026-01-01", "2026-02-19", "day", 1, bars);

        Assert.True(result.Id > 0);
        var saved = context.StrategyExecutions.FirstOrDefault(e => e.Id == result.Id);
        Assert.NotNull(saved);
        Assert.Equal("sma_crossover", saved.StrategyName);
    }

    [Fact]
    public async Task RunBacktestAsync_DefaultParameters_UsedWhenJsonEmpty()
    {
        var service = CreateService();

        var closes = Enumerable.Range(0, 50)
            .Select(i => 100m + i * 0.5m)
            .ToArray();
        var bars = CreateBars(closes, new DateTime(2026, 1, 1, 0, 0, 0, DateTimeKind.Utc));

        // Empty JSON should use defaults (shortWindow=10, longWindow=30)
        var result = await service.RunBacktestAsync(
            1, "sma_crossover", "{}",
            "2026-01-01", "2026-02-19", "day", 1, bars);

        Assert.NotNull(result);
        Assert.Equal("sma_crossover", result.StrategyName);
    }

    #endregion

    #region Strategy logic: trade counting and PnL

    [Fact]
    public async Task RunBacktestAsync_SmaCrossover_WinningAndLosingTradesCounted()
    {
        var service = CreateService();

        // Create a pattern that generates a crossover trade
        var closes = Enumerable.Range(0, 50)
            .Select(i => 100m + (decimal)Math.Sin(i * 0.3) * 10m)
            .ToArray();
        var bars = CreateBars(closes, new DateTime(2026, 1, 1, 0, 0, 0, DateTimeKind.Utc));

        var result = await service.RunBacktestAsync(
            1, "sma_crossover", """{"ShortWindow":3,"LongWindow":8}""",
            "2026-01-01", "2026-02-19", "day", 1, bars);

        Assert.Equal(result.WinningTrades + result.LosingTrades, result.TotalTrades);
        Assert.Equal(result.Trades.Sum(t => t.PnL), result.TotalPnL);
    }

    [Fact]
    public async Task RunBacktestAsync_CumulativePnLTracksCorrectly()
    {
        var service = CreateService();

        var closes = Enumerable.Range(0, 50)
            .Select(i => 100m + (decimal)Math.Sin(i * 0.3) * 10m)
            .ToArray();
        var bars = CreateBars(closes, new DateTime(2026, 1, 1, 0, 0, 0, DateTimeKind.Utc));

        var result = await service.RunBacktestAsync(
            1, "sma_crossover", """{"ShortWindow":3,"LongWindow":8}""",
            "2026-01-01", "2026-02-19", "day", 1, bars);

        if (result.Trades.Count > 0)
        {
            var lastTrade = result.Trades.Last();
            Assert.Equal(result.TotalPnL, lastTrade.CumulativePnL);
        }
    }

    #endregion

    #region Metrics: Sharpe and Drawdown

    [Fact]
    public async Task RunBacktestAsync_MaxDrawdownIsNonNegative()
    {
        var service = CreateService();

        var closes = Enumerable.Range(0, 50)
            .Select(i => 100m + (decimal)Math.Sin(i * 0.3) * 10m)
            .ToArray();
        var bars = CreateBars(closes, new DateTime(2026, 1, 1, 0, 0, 0, DateTimeKind.Utc));

        var result = await service.RunBacktestAsync(
            1, "sma_crossover", """{"ShortWindow":3,"LongWindow":8}""",
            "2026-01-01", "2026-02-19", "day", 1, bars);

        Assert.True(result.MaxDrawdown >= 0);
    }

    [Fact]
    public async Task RunBacktestAsync_ZeroTrades_SharpeIsZero()
    {
        var service = CreateService();
        var bars = CreateBars([100m, 101m, 102m, 103m, 104m],
            new DateTime(2026, 1, 1, 0, 0, 0, DateTimeKind.Utc));

        var result = await service.RunBacktestAsync(
            1, "sma_crossover", """{"ShortWindow":10,"LongWindow":30}""",
            "2026-01-01", "2026-01-05", "day", 1, bars);

        Assert.Equal(0m, result.SharpeRatio);
    }

    #endregion

    #region Input sorting

    [Fact]
    public async Task RunBacktestAsync_SortsBarsBeforeProcessing()
    {
        var service = CreateService();

        var closes = Enumerable.Range(0, 50)
            .Select(i => 100m + i * 0.5m)
            .ToArray();
        var bars = CreateBars(closes, new DateTime(2026, 1, 1, 0, 0, 0, DateTimeKind.Utc));

        // Reverse the bars to simulate unsorted input
        bars.Reverse();

        var result = await service.RunBacktestAsync(
            1, "sma_crossover", """{"ShortWindow":5,"LongWindow":10}""",
            "2026-01-01", "2026-02-19", "day", 1, bars);

        // Should still produce valid results (bars get sorted internally)
        Assert.NotNull(result);
    }

    #endregion
}

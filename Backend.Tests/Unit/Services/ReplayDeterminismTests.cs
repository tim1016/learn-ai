using Backend.Models.MarketData;
using Backend.Services.Implementation;
using Backend.Tests.Helpers;
using Microsoft.Extensions.Logging;
using Moq;

namespace Backend.Tests.Unit.Services;

public class ReplayDeterminismTests
{
    private readonly Mock<ILogger<BacktestService>> _loggerMock = new();

    private BacktestService CreateService()
    {
        var context = TestDbContextFactory.Create();
        return new BacktestService(context, _loggerMock.Object);
    }

    private static List<StockAggregate> CreateTrendingBars(int count, DateTime startDate)
    {
        // Creates bars with a clear trend pattern that produces SMA crossover signals
        var bars = new List<StockAggregate>();
        for (var i = 0; i < count; i++)
        {
            // First half trending down, second half trending up — creates crossovers
            var close = i < count / 2
                ? 200m - i * 1.5m
                : 200m - (count / 2) * 1.5m + (i - count / 2) * 2m;

            bars.Add(new StockAggregate
            {
                TickerId = 1,
                Open = close - 1,
                High = close + 2,
                Low = close - 2,
                Close = close,
                Volume = 1_000_000,
                Timestamp = startDate.AddMinutes(i),
                Timespan = "minute",
                Multiplier = 1,
            });
        }
        return bars;
    }

    private static List<StockAggregate> CreateOscillatingBars(int count, DateTime startDate)
    {
        // Creates bars that oscillate enough to trigger RSI signals
        var bars = new List<StockAggregate>();
        for (var i = 0; i < count; i++)
        {
            // Sine wave pattern with enough amplitude to trigger RSI oversold/overbought
            var close = 150m + (decimal)Math.Sin(i * 0.15) * 20m;

            bars.Add(new StockAggregate
            {
                TickerId = 1,
                Open = close - 1,
                High = close + 2,
                Low = close - 2,
                Close = close,
                Volume = 1_000_000,
                Timestamp = startDate.AddMinutes(i),
                Timespan = "minute",
                Multiplier = 1,
            });
        }
        return bars;
    }

    #region SMA Crossover Determinism

    [Fact]
    public async Task SmaCrossover_Determinism_SameInputProducesSameOutput()
    {
        var bars = CreateTrendingBars(100, new DateTime(2026, 1, 5, 9, 30, 0, DateTimeKind.Utc));
        var parameters = """{"ShortWindow":5,"LongWindow":15}""";

        // Run 1
        var service1 = CreateService();
        var result1 = await service1.RunBacktestAsync(
            1, "sma_crossover", parameters, "2026-01-05", "2026-01-05", "minute", 1, bars);

        // Run 2 (same input, fresh service)
        var service2 = CreateService();
        var result2 = await service2.RunBacktestAsync(
            1, "sma_crossover", parameters, "2026-01-05", "2026-01-05", "minute", 1, bars);

        // Determinism: identical trade lists
        Assert.Equal(result1.TotalTrades, result2.TotalTrades);
        Assert.Equal(result1.TotalPnL, result2.TotalPnL);
        Assert.Equal(result1.WinningTrades, result2.WinningTrades);
        Assert.Equal(result1.LosingTrades, result2.LosingTrades);
        Assert.Equal(result1.MaxDrawdown, result2.MaxDrawdown);
        Assert.Equal(result1.SharpeRatio, result2.SharpeRatio);

        for (var i = 0; i < result1.Trades.Count; i++)
        {
            Assert.Equal(result1.Trades[i].EntryTimestamp, result2.Trades[i].EntryTimestamp);
            Assert.Equal(result1.Trades[i].ExitTimestamp, result2.Trades[i].ExitTimestamp);
            Assert.Equal(result1.Trades[i].EntryPrice, result2.Trades[i].EntryPrice);
            Assert.Equal(result1.Trades[i].ExitPrice, result2.Trades[i].ExitPrice);
            Assert.Equal(result1.Trades[i].PnL, result2.Trades[i].PnL);
            Assert.Equal(result1.Trades[i].CumulativePnL, result2.Trades[i].CumulativePnL);
        }
    }

    [Fact]
    public async Task SmaCrossover_NoLookahead_SignalsOnlyUsePastData()
    {
        var bars = CreateTrendingBars(100, new DateTime(2026, 1, 5, 9, 30, 0, DateTimeKind.Utc));
        var parameters = """{"ShortWindow":5,"LongWindow":15}""";

        var service = CreateService();
        var result = await service.RunBacktestAsync(
            1, "sma_crossover", parameters, "2026-01-05", "2026-01-05", "minute", 1, bars);

        var sortedBars = bars.OrderBy(b => b.Timestamp).ToList();
        const int longWindow = 15;

        // Every trade's entry must be at index >= longWindow (warmup period)
        foreach (var trade in result.Trades)
        {
            var entryBarIndex = sortedBars.FindIndex(b => b.Timestamp == trade.EntryTimestamp);
            Assert.True(entryBarIndex >= longWindow - 1,
                $"Trade entry at index {entryBarIndex} is before warmup period (longWindow={longWindow})");
        }

        // Exit must be after entry
        foreach (var trade in result.Trades)
        {
            Assert.True(trade.ExitTimestamp >= trade.EntryTimestamp,
                "Trade exit timestamp must be >= entry timestamp");
        }
    }

    #endregion

    #region RSI Mean Reversion Determinism

    [Fact]
    public async Task RsiMeanReversion_Determinism_SameInputProducesSameOutput()
    {
        var bars = CreateOscillatingBars(200, new DateTime(2026, 1, 5, 9, 30, 0, DateTimeKind.Utc));
        var parameters = """{"Window":14,"Oversold":30,"Overbought":70}""";

        // Run 1
        var service1 = CreateService();
        var result1 = await service1.RunBacktestAsync(
            1, "rsi_mean_reversion", parameters, "2026-01-05", "2026-01-05", "minute", 1, bars);

        // Run 2 (same input, fresh service)
        var service2 = CreateService();
        var result2 = await service2.RunBacktestAsync(
            1, "rsi_mean_reversion", parameters, "2026-01-05", "2026-01-05", "minute", 1, bars);

        // Determinism: identical results
        Assert.Equal(result1.TotalTrades, result2.TotalTrades);
        Assert.Equal(result1.TotalPnL, result2.TotalPnL);
        Assert.Equal(result1.WinningTrades, result2.WinningTrades);
        Assert.Equal(result1.LosingTrades, result2.LosingTrades);
        Assert.Equal(result1.MaxDrawdown, result2.MaxDrawdown);
        Assert.Equal(result1.SharpeRatio, result2.SharpeRatio);

        for (var i = 0; i < result1.Trades.Count; i++)
        {
            Assert.Equal(result1.Trades[i].EntryTimestamp, result2.Trades[i].EntryTimestamp);
            Assert.Equal(result1.Trades[i].ExitTimestamp, result2.Trades[i].ExitTimestamp);
            Assert.Equal(result1.Trades[i].PnL, result2.Trades[i].PnL);
        }
    }

    [Fact]
    public async Task RsiMeanReversion_NoLookahead_SignalsOnlyUsePastData()
    {
        var bars = CreateOscillatingBars(200, new DateTime(2026, 1, 5, 9, 30, 0, DateTimeKind.Utc));
        var parameters = """{"Window":14,"Oversold":30,"Overbought":70}""";

        var service = CreateService();
        var result = await service.RunBacktestAsync(
            1, "rsi_mean_reversion", parameters, "2026-01-05", "2026-01-05", "minute", 1, bars);

        var sortedBars = bars.OrderBy(b => b.Timestamp).ToList();
        const int rsiWindow = 14;

        // Every trade's entry must be after RSI warmup period
        foreach (var trade in result.Trades)
        {
            var entryBarIndex = sortedBars.FindIndex(b => b.Timestamp == trade.EntryTimestamp);
            Assert.True(entryBarIndex >= rsiWindow,
                $"RSI trade entry at index {entryBarIndex} is before warmup period (window={rsiWindow})");
        }

        // Exit must be after entry
        foreach (var trade in result.Trades)
        {
            Assert.True(trade.ExitTimestamp >= trade.EntryTimestamp,
                "Trade exit timestamp must be >= entry timestamp");
        }
    }

    #endregion

    #region Input Order Independence

    [Fact]
    public async Task SmaCrossover_ShuffledInput_ProducesIdenticalResults()
    {
        var bars = CreateTrendingBars(100, new DateTime(2026, 1, 5, 9, 30, 0, DateTimeKind.Utc));
        var parameters = """{"ShortWindow":5,"LongWindow":15}""";

        // Run with original order
        var service1 = CreateService();
        var result1 = await service1.RunBacktestAsync(
            1, "sma_crossover", parameters, "2026-01-05", "2026-01-05", "minute", 1, bars);

        // Shuffle bars (service should sort internally)
        var shuffled = bars.OrderBy(_ => Guid.NewGuid()).ToList();
        var service2 = CreateService();
        var result2 = await service2.RunBacktestAsync(
            1, "sma_crossover", parameters, "2026-01-05", "2026-01-05", "minute", 1, shuffled);

        Assert.Equal(result1.TotalTrades, result2.TotalTrades);
        Assert.Equal(result1.TotalPnL, result2.TotalPnL);

        for (var i = 0; i < result1.Trades.Count; i++)
        {
            Assert.Equal(result1.Trades[i].EntryPrice, result2.Trades[i].EntryPrice);
            Assert.Equal(result1.Trades[i].ExitPrice, result2.Trades[i].ExitPrice);
            Assert.Equal(result1.Trades[i].PnL, result2.Trades[i].PnL);
        }
    }

    #endregion
}

using System.Diagnostics;
using System.Text.Json;
using Backend.Data;
using Backend.Models.MarketData;
using Backend.Services.Interfaces;

namespace Backend.Services.Implementation;

public class BacktestService : IBacktestService
{
    private readonly AppDbContext _context;
    private readonly ILogger<BacktestService> _logger;

    public BacktestService(AppDbContext context, ILogger<BacktestService> logger)
    {
        _context = context;
        _logger = logger;
    }

    public async Task<StrategyExecution> RunBacktestAsync(
        int tickerId,
        string strategyName,
        string parametersJson,
        string startDate,
        string endDate,
        string timespan,
        int multiplier,
        List<StockAggregate> bars,
        CancellationToken cancellationToken = default)
    {
        var sw = Stopwatch.StartNew();

        var sortedBars = bars.OrderBy(b => b.Timestamp).ToList();
        if (sortedBars.Count < 2)
        {
            throw new InvalidOperationException("Need at least 2 bars to run a backtest");
        }

        var trades = strategyName.ToLowerInvariant() switch
        {
            "sma_crossover" => RunSmaCrossover(sortedBars, parametersJson),
            "rsi_mean_reversion" => RunRsiMeanReversion(sortedBars, parametersJson),
            _ => throw new ArgumentException($"Unknown strategy: {strategyName}")
        };

        sw.Stop();

        var winning = trades.Count(t => t.PnL > 0);
        var losing = trades.Count(t => t.PnL <= 0);
        var totalPnl = trades.Sum(t => t.PnL);
        var maxDrawdown = CalculateMaxDrawdown(trades);
        var sharpe = CalculateSharpeRatio(trades);

        var execution = new StrategyExecution
        {
            TickerId = tickerId,
            StrategyName = strategyName,
            Parameters = parametersJson,
            StartDate = startDate,
            EndDate = endDate,
            Timespan = timespan,
            Multiplier = multiplier,
            TotalTrades = trades.Count,
            WinningTrades = winning,
            LosingTrades = losing,
            TotalPnL = totalPnl,
            MaxDrawdown = maxDrawdown,
            SharpeRatio = sharpe,
            ExecutedAt = DateTime.UtcNow,
            DurationMs = sw.ElapsedMilliseconds,
            Trades = trades,
        };

        _context.StrategyExecutions.Add(execution);
        await _context.SaveChangesAsync(cancellationToken);

        _logger.LogInformation(
            "[Backtest] {Strategy} on TickerId={TickerId}: {Trades} trades, PnL={PnL:F2}, Duration={Ms}ms",
            strategyName, tickerId, trades.Count, totalPnl, sw.ElapsedMilliseconds);

        return execution;
    }

    private static List<BacktestTrade> RunSmaCrossover(
        List<StockAggregate> bars, string parametersJson)
    {
        var p = JsonSerializer.Deserialize<SmaCrossoverParams>(parametersJson)
            ?? new SmaCrossoverParams();

        var shortWindow = p.ShortWindow > 0 ? p.ShortWindow : 10;
        var longWindow = p.LongWindow > 0 ? p.LongWindow : 30;

        if (bars.Count < longWindow)
            return [];

        var closes = bars.Select(b => b.Close).ToList();
        var shortSma = ComputeSma(closes, shortWindow);
        var longSma = ComputeSma(closes, longWindow);

        var trades = new List<BacktestTrade>();
        bool inPosition = false;
        int entryIndex = 0;
        decimal cumulativePnl = 0;

        // Start from longWindow where both SMAs are valid
        for (int i = longWindow; i < bars.Count; i++)
        {
            var prevShort = shortSma[i - 1];
            var prevLong = longSma[i - 1];
            var currShort = shortSma[i];
            var currLong = longSma[i];

            // Golden cross: short crosses above long → buy
            if (!inPosition && prevShort <= prevLong && currShort > currLong)
            {
                inPosition = true;
                entryIndex = i;
            }
            // Death cross: short crosses below long → sell
            else if (inPosition && prevShort >= prevLong && currShort < currLong)
            {
                var pnl = bars[i].Close - bars[entryIndex].Close;
                cumulativePnl += pnl;

                trades.Add(new BacktestTrade
                {
                    TradeType = "Buy",
                    EntryTimestamp = bars[entryIndex].Timestamp,
                    ExitTimestamp = bars[i].Timestamp,
                    EntryPrice = bars[entryIndex].Close,
                    ExitPrice = bars[i].Close,
                    Quantity = 1,
                    PnL = pnl,
                    CumulativePnL = cumulativePnl,
                    SignalReason = $"SMA({shortWindow}) crossed below SMA({longWindow})",
                });

                inPosition = false;
            }
        }

        // Close open position at end
        if (inPosition)
        {
            var lastIdx = bars.Count - 1;
            var pnl = bars[lastIdx].Close - bars[entryIndex].Close;
            cumulativePnl += pnl;

            trades.Add(new BacktestTrade
            {
                TradeType = "Buy",
                EntryTimestamp = bars[entryIndex].Timestamp,
                ExitTimestamp = bars[lastIdx].Timestamp,
                EntryPrice = bars[entryIndex].Close,
                ExitPrice = bars[lastIdx].Close,
                Quantity = 1,
                PnL = pnl,
                CumulativePnL = cumulativePnl,
                SignalReason = "Position closed at end of period",
            });
        }

        return trades;
    }

    private static List<BacktestTrade> RunRsiMeanReversion(
        List<StockAggregate> bars, string parametersJson)
    {
        var p = JsonSerializer.Deserialize<RsiMeanReversionParams>(parametersJson)
            ?? new RsiMeanReversionParams();

        var window = p.Window > 0 ? p.Window : 14;
        var oversold = p.Oversold > 0 ? p.Oversold : 30m;
        var overbought = p.Overbought > 0 ? p.Overbought : 70m;

        if (bars.Count < window + 1)
            return [];

        var rsi = ComputeRsi(bars.Select(b => b.Close).ToList(), window);

        var trades = new List<BacktestTrade>();
        bool inPosition = false;
        int entryIndex = 0;
        decimal cumulativePnl = 0;

        for (int i = window + 1; i < bars.Count; i++)
        {
            if (rsi[i] == null) continue;

            // Buy when RSI drops below oversold
            if (!inPosition && rsi[i]!.Value < oversold)
            {
                inPosition = true;
                entryIndex = i;
            }
            // Sell when RSI rises above overbought
            else if (inPosition && rsi[i]!.Value > overbought)
            {
                var pnl = bars[i].Close - bars[entryIndex].Close;
                cumulativePnl += pnl;

                trades.Add(new BacktestTrade
                {
                    TradeType = "Buy",
                    EntryTimestamp = bars[entryIndex].Timestamp,
                    ExitTimestamp = bars[i].Timestamp,
                    EntryPrice = bars[entryIndex].Close,
                    ExitPrice = bars[i].Close,
                    Quantity = 1,
                    PnL = pnl,
                    CumulativePnL = cumulativePnl,
                    SignalReason = $"RSI({window}) crossed above {overbought}",
                });

                inPosition = false;
            }
        }

        // Close open position at end
        if (inPosition)
        {
            var lastIdx = bars.Count - 1;
            var pnl = bars[lastIdx].Close - bars[entryIndex].Close;
            cumulativePnl += pnl;

            trades.Add(new BacktestTrade
            {
                TradeType = "Buy",
                EntryTimestamp = bars[entryIndex].Timestamp,
                ExitTimestamp = bars[lastIdx].Timestamp,
                EntryPrice = bars[entryIndex].Close,
                ExitPrice = bars[lastIdx].Close,
                Quantity = 1,
                PnL = pnl,
                CumulativePnL = cumulativePnl,
                SignalReason = "Position closed at end of period",
            });
        }

        return trades;
    }

    private static List<decimal> ComputeSma(List<decimal> values, int window)
    {
        var sma = new List<decimal>(new decimal[values.Count]);
        for (int i = window - 1; i < values.Count; i++)
        {
            decimal sum = 0;
            for (int j = i - window + 1; j <= i; j++)
                sum += values[j];
            sma[i] = sum / window;
        }
        return sma;
    }

    private static List<decimal?> ComputeRsi(List<decimal> closes, int window)
    {
        var rsi = new List<decimal?>(new decimal?[closes.Count]);
        if (closes.Count < window + 1) return rsi;

        decimal avgGain = 0, avgLoss = 0;

        // Initial averages
        for (int i = 1; i <= window; i++)
        {
            var change = closes[i] - closes[i - 1];
            if (change > 0) avgGain += change;
            else avgLoss += Math.Abs(change);
        }
        avgGain /= window;
        avgLoss /= window;

        rsi[window] = avgLoss == 0 ? 100m : 100m - (100m / (1m + avgGain / avgLoss));

        // Smoothed averages
        for (int i = window + 1; i < closes.Count; i++)
        {
            var change = closes[i] - closes[i - 1];
            var gain = change > 0 ? change : 0;
            var loss = change < 0 ? Math.Abs(change) : 0;

            avgGain = (avgGain * (window - 1) + gain) / window;
            avgLoss = (avgLoss * (window - 1) + loss) / window;

            rsi[i] = avgLoss == 0 ? 100m : 100m - (100m / (1m + avgGain / avgLoss));
        }

        return rsi;
    }

    private static decimal CalculateMaxDrawdown(List<BacktestTrade> trades)
    {
        if (trades.Count == 0) return 0;

        decimal peak = 0;
        decimal maxDrawdown = 0;

        foreach (var trade in trades)
        {
            if (trade.CumulativePnL > peak)
                peak = trade.CumulativePnL;

            var drawdown = peak - trade.CumulativePnL;
            if (drawdown > maxDrawdown)
                maxDrawdown = drawdown;
        }

        return maxDrawdown;
    }

    private static decimal CalculateSharpeRatio(List<BacktestTrade> trades)
    {
        if (trades.Count < 2) return 0;

        var returns = trades.Select(t => t.PnL).ToList();
        var avg = returns.Average();
        var variance = returns.Sum(r => (r - avg) * (r - avg)) / (returns.Count - 1);
        var stdDev = (decimal)Math.Sqrt((double)variance);

        return stdDev == 0 ? 0 : Math.Round(avg / stdDev * (decimal)Math.Sqrt(252), 4);
    }

    private record SmaCrossoverParams
    {
        public int ShortWindow { get; init; } = 10;
        public int LongWindow { get; init; } = 30;
    }

    private record RsiMeanReversionParams
    {
        public int Window { get; init; } = 14;
        public decimal Oversold { get; init; } = 30;
        public decimal Overbought { get; init; } = 70;
    }
}

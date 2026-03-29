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
            "momentum_rsi_stochastic" => RunMomentumRsiStochastic(sortedBars, parametersJson),
            "rsi_reversal" => RunRsiReversal(sortedBars, parametersJson),
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

    /// <summary>
    /// Public entry point for running a strategy on raw bars without DB persistence.
    /// Used by the CSV import mutation.
    /// </summary>
    public static List<BacktestTrade> RunStrategy(
        List<StockAggregate> bars, string strategyName, string parametersJson)
    {
        return strategyName.ToLowerInvariant() switch
        {
            "sma_crossover" => RunSmaCrossover(bars, parametersJson),
            "rsi_mean_reversion" => RunRsiMeanReversion(bars, parametersJson),
            "momentum_rsi_stochastic" => RunMomentumRsiStochastic(bars, parametersJson),
            "rsi_reversal" => RunRsiReversal(bars, parametersJson),
            _ => throw new ArgumentException($"Unknown strategy: {strategyName}")
        };
    }

    public static decimal CalcMaxDrawdown(List<BacktestTrade> trades) => CalculateMaxDrawdown(trades);
    public static decimal CalcSharpe(List<BacktestTrade> trades) => CalculateSharpeRatio(trades);

    private static readonly TimeZoneInfo EasternTz =
        TimeZoneInfo.FindSystemTimeZoneById("America/New_York");

    /// <summary>
    /// Filter bars to US Regular Trading Hours only (9:30 AM – 4:00 PM Eastern).
    /// Removes pre-market and after-hours bars. Handles EST/EDT automatically.
    /// </summary>
    public static List<StockAggregate> FilterToRegularHours(List<StockAggregate> bars)
    {
        var marketOpen = new TimeSpan(9, 30, 0);
        var marketClose = new TimeSpan(16, 0, 0);

        return bars.Where(b =>
        {
            var eastern = TimeZoneInfo.ConvertTimeFromUtc(
                DateTime.SpecifyKind(b.Timestamp, DateTimeKind.Utc), EasternTz);
            var tod = eastern.TimeOfDay;
            return tod >= marketOpen && tod < marketClose;
        }).ToList();
    }

    /// <summary>
    /// Resample minute bars to a larger timeframe (e.g. 5m, 15m, 30m, 60m).
    /// Bars are grouped into windows of <paramref name="minutes"/> minutes based on timestamp.
    /// </summary>
    public static List<StockAggregate> ResampleBars(List<StockAggregate> minuteBars, int minutes)
    {
        if (minutes <= 1) return minuteBars;

        var sorted = minuteBars.OrderBy(b => b.Timestamp).ToList();
        if (sorted.Count == 0) return sorted;

        var resampled = new List<StockAggregate>();
        var groups = sorted.GroupBy(b =>
        {
            // Group by trading day + floored time bucket
            var ts = b.Timestamp;
            var dayStart = ts.Date;
            var minutesSinceMidnight = (int)ts.TimeOfDay.TotalMinutes;
            var bucket = minutesSinceMidnight / minutes * minutes;
            return dayStart.AddMinutes(bucket);
        });

        foreach (var group in groups.OrderBy(g => g.Key))
        {
            var bars = group.ToList();
            resampled.Add(new StockAggregate
            {
                Id = bars[0].Id,
                TickerId = bars[0].TickerId,
                Open = bars[0].Open,
                High = bars.Max(b => b.High),
                Low = bars.Min(b => b.Low),
                Close = bars[^1].Close,
                Volume = bars.Sum(b => b.Volume),
                VolumeWeightedAveragePrice = null,
                Timestamp = group.Key,
                Timespan = $"{minutes}min",
                Multiplier = minutes,
                TransactionCount = null,
            });
        }

        return resampled;
    }

    /// <summary>
    /// Parse a timeframe string like "1m", "5m", "15m", "30m", "1h" into minutes.
    /// </summary>
    public static int ParseTimeframeMinutes(string timeframe)
    {
        if (string.IsNullOrWhiteSpace(timeframe)) return 1;

        var tf = timeframe.Trim().ToLowerInvariant();
        if (tf.EndsWith("m") && int.TryParse(tf[..^1], out var mins)) return mins;
        if (tf.EndsWith("h") && int.TryParse(tf[..^1], out var hrs)) return hrs * 60;
        if (tf == "minute") return 1;
        if (tf == "hour") return 60;

        return 1;
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

    private static List<BacktestTrade> RunMomentumRsiStochastic(
        List<StockAggregate> bars, string parametersJson)
    {
        var p = JsonSerializer.Deserialize<MomentumRsiStochasticParams>(parametersJson)
            ?? new MomentumRsiStochasticParams();

        var rsiLength = p.RsiLength > 0 ? p.RsiLength : 14;
        var rsiLow = p.RsiLow > 0 ? p.RsiLow : 40m;
        var rsiHigh = p.RsiHigh > 0 ? p.RsiHigh : 60m;
        var fastMa = p.FastMa > 0 ? p.FastMa : 20;
        var slowMa = p.SlowMa > 0 ? p.SlowMa : 50;
        var stochK = p.StochK > 0 ? p.StochK : 14;
        var stochD = p.StochD > 0 ? p.StochD : 3;
        var exitMinutesBefore = p.ExitMinutesBefore > 0 ? p.ExitMinutesBefore : 15;

        var minBars = Math.Max(slowMa, Math.Max(rsiLength, stochK + stochD)) + 1;
        if (bars.Count < minBars)
            return [];

        var closes = bars.Select(b => b.Close).ToList();
        var highs = bars.Select(b => b.High).ToList();
        var lows = bars.Select(b => b.Low).ToList();

        var rsi = ComputeRsi(closes, rsiLength);
        var fastSma = ComputeSma(closes, fastMa);
        var slowSma = ComputeSma(closes, slowMa);
        var (percentK, percentD) = ComputeStochastic(highs, lows, closes, stochK, stochD);

        var trades = new List<BacktestTrade>();
        bool inPosition = false;
        int entryIndex = 0;
        decimal cumulativePnl = 0;

        // Pre-compute the last bar timestamp for each trading day
        // This avoids timezone issues — we detect EOD relative to actual data
        var lastBarByDay = new Dictionary<DateOnly, DateTime>();
        foreach (var bar in bars)
        {
            var day = DateOnly.FromDateTime(bar.Timestamp);
            if (!lastBarByDay.ContainsKey(day) || bar.Timestamp > lastBarByDay[day])
                lastBarByDay[day] = bar.Timestamp;
        }

        for (int i = minBars; i < bars.Count; i++)
        {
            var barTime = bars[i].Timestamp;

            // Exit check: close position N minutes before the last bar of this trading day
            if (inPosition)
            {
                var currentDay = DateOnly.FromDateTime(barTime);
                var dayLastBar = lastBarByDay[currentDay];
                bool isEodExit = barTime >= dayLastBar.AddMinutes(-exitMinutesBefore);
                bool isLastBar = i == bars.Count - 1;

                if (isEodExit || isLastBar)
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
                        SignalReason = isEodExit
                            ? $"EOD exit {exitMinutesBefore}min before close"
                            : "Position closed at end of period",
                    });

                    inPosition = false;
                    continue;
                }
            }

            // Entry check: all conditions must be true
            if (!inPosition
                && rsi[i] != null
                && rsi[i]!.Value >= rsiLow && rsi[i]!.Value <= rsiHigh
                && fastSma[i] > 0 && slowSma[i] > 0
                && fastSma[i] > slowSma[i]
                && closes[i] > fastSma[i]
                && percentK[i] != null && percentD[i] != null
                && percentK[i]!.Value > percentD[i]!.Value)
            {
                // Enter at next bar open (if available)
                if (i + 1 < bars.Count)
                {
                    inPosition = true;
                    entryIndex = i + 1;
                }
            }
        }

        return trades;
    }

    /// <summary>
    /// RSI Reversal strategy — always-in-the-market, flips between long and short.
    /// Matches TradingView's built-in "RSI Strategy" behavior.
    /// When RSI crosses below oversold → close short, open long (RsiLE).
    /// When RSI crosses above overbought → close long, open short (RsiSE).
    /// </summary>
    private static List<BacktestTrade> RunRsiReversal(
        List<StockAggregate> bars, string parametersJson)
    {
        var p = JsonSerializer.Deserialize<RsiReversalParams>(parametersJson)
            ?? new RsiReversalParams();

        var window = p.Window > 0 ? p.Window : 14;
        var oversold = p.Oversold > 0 ? p.Oversold : 30m;
        var overbought = p.Overbought > 0 ? p.Overbought : 70m;

        if (bars.Count < window + 2)
            return [];

        var rsi = ComputeRsi(bars.Select(b => b.Close).ToList(), window);

        var trades = new List<BacktestTrade>();
        string? positionType = null; // "Long" or "Short"
        int entryIndex = 0;
        decimal cumulativePnl = 0;

        for (int i = window + 1; i < bars.Count; i++)
        {
            if (rsi[i] == null || rsi[i - 1] == null) continue;

            var prevRsi = rsi[i - 1]!.Value;
            var currRsi = rsi[i]!.Value;

            // RsiLE: RSI crosses below oversold → close short (if any), open long
            if (prevRsi >= oversold && currRsi < oversold)
            {
                // Close existing short position
                if (positionType == "Short")
                {
                    var pnl = bars[entryIndex].Close - bars[i].Close; // short PnL = entry - exit
                    cumulativePnl += pnl;

                    trades.Add(new BacktestTrade
                    {
                        TradeType = "Sell",
                        EntryTimestamp = bars[entryIndex].Timestamp,
                        ExitTimestamp = bars[i].Timestamp,
                        EntryPrice = bars[entryIndex].Close,
                        ExitPrice = bars[i].Close,
                        Quantity = 1,
                        PnL = pnl,
                        CumulativePnL = cumulativePnl,
                        SignalReason = $"RsiLE: RSI({window}) crossed below {oversold}",
                    });
                }

                // Open long
                positionType = "Long";
                entryIndex = i;
            }
            // RsiSE: RSI crosses above overbought → close long (if any), open short
            else if (prevRsi <= overbought && currRsi > overbought)
            {
                // Close existing long position
                if (positionType == "Long")
                {
                    var pnl = bars[i].Close - bars[entryIndex].Close; // long PnL = exit - entry
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
                        SignalReason = $"RsiSE: RSI({window}) crossed above {overbought}",
                    });
                }

                // Open short
                positionType = "Short";
                entryIndex = i;
            }
        }

        // Close open position at end
        if (positionType != null)
        {
            var lastIdx = bars.Count - 1;
            var pnl = positionType == "Long"
                ? bars[lastIdx].Close - bars[entryIndex].Close
                : bars[entryIndex].Close - bars[lastIdx].Close;
            cumulativePnl += pnl;

            trades.Add(new BacktestTrade
            {
                TradeType = positionType == "Long" ? "Buy" : "Sell",
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

    private static (List<decimal?> percentK, List<decimal?> percentD) ComputeStochastic(
        List<decimal> highs, List<decimal> lows, List<decimal> closes, int kPeriod, int dPeriod)
    {
        var rawK = new List<decimal?>(new decimal?[closes.Count]);

        for (int i = kPeriod - 1; i < closes.Count; i++)
        {
            decimal highestHigh = decimal.MinValue;
            decimal lowestLow = decimal.MaxValue;

            for (int j = i - kPeriod + 1; j <= i; j++)
            {
                if (highs[j] > highestHigh) highestHigh = highs[j];
                if (lows[j] < lowestLow) lowestLow = lows[j];
            }

            var range = highestHigh - lowestLow;
            rawK[i] = range == 0 ? 50m : (closes[i] - lowestLow) / range * 100m;
        }

        // %D is SMA of %K over dPeriod
        var percentD = new List<decimal?>(new decimal?[closes.Count]);
        for (int i = kPeriod - 1 + dPeriod - 1; i < closes.Count; i++)
        {
            decimal sum = 0;
            int count = 0;
            for (int j = i - dPeriod + 1; j <= i; j++)
            {
                if (rawK[j] != null)
                {
                    sum += rawK[j]!.Value;
                    count++;
                }
            }
            if (count == dPeriod)
                percentD[i] = sum / dPeriod;
        }

        return (rawK, percentD);
    }

    private record MomentumRsiStochasticParams
    {
        public int RsiLength { get; init; } = 14;
        public decimal RsiLow { get; init; } = 40;
        public decimal RsiHigh { get; init; } = 60;
        public int FastMa { get; init; } = 20;
        public int SlowMa { get; init; } = 50;
        public int StochK { get; init; } = 14;
        public int StochD { get; init; } = 3;
        public int ExitMinutesBefore { get; init; } = 15;
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

    private record RsiReversalParams
    {
        public int Window { get; init; } = 14;
        public decimal Oversold { get; init; } = 30;
        public decimal Overbought { get; init; } = 70;
    }
}

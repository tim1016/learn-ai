using System.Text.Json;
using Backend.Data;
using Backend.Models.MarketData;
using Backend.Services.Interfaces;
using Microsoft.EntityFrameworkCore;

namespace Backend.Services.Implementation;

public class BacktestRunPersistenceService : IBacktestRunPersistenceService
{
    private readonly AppDbContext _db;
    private readonly ILogger<BacktestRunPersistenceService> _logger;

    public BacktestRunPersistenceService(AppDbContext db, ILogger<BacktestRunPersistenceService> logger)
    {
        _db = db;
        _logger = logger;
    }

    public async Task<int> PersistAsync(PersistLeanRunPayload payload, CancellationToken ct)
    {
        if (payload.Source != "lean-sidecar")
        {
            throw new ArgumentException(
                $"Expected source='lean-sidecar', got '{payload.Source}'",
                nameof(payload));
        }

        // Idempotency: re-running with same LeanRunId returns existing Id.
        var existing = await _db.StrategyExecutions
            .AsNoTracking()
            .Where(s => s.Source == "lean-sidecar" && s.LeanRunId == payload.LeanRunId)
            .Select(s => (int?)s.Id)
            .FirstOrDefaultAsync(ct);

        if (existing.HasValue)
        {
            _logger.LogInformation(
                "[STEP 1] PersistLean idempotent: LeanRunId={LeanRunId} already exists as StrategyExecutionId={Id}",
                payload.LeanRunId, existing.Value);
            return existing.Value;
        }

        // Resolve or create the Ticker entity for this symbol.
        var symbol = payload.Symbol.ToUpperInvariant();
        var ticker = await _db.Tickers
            .FirstOrDefaultAsync(t => t.Symbol == symbol, ct);

        if (ticker == null)
        {
            ticker = new Ticker { Symbol = symbol, Name = symbol, Market = "stocks" };
            _db.Tickers.Add(ticker);
            await _db.SaveChangesAsync(ct);
            _logger.LogInformation("[STEP 2] Created Ticker for Symbol={Symbol}", symbol);
        }

        var startDateStr = DateTimeOffset.FromUnixTimeMilliseconds(payload.StartDateMs)
            .UtcDateTime.ToString("yyyy-MM-dd");
        var endDateStr = DateTimeOffset.FromUnixTimeMilliseconds(payload.EndDateMs)
            .UtcDateTime.ToString("yyyy-MM-dd");

        var execution = new StrategyExecution
        {
            TickerId = ticker.Id,
            StrategyName = payload.StrategyName,
            Parameters = JsonSerializer.Serialize(new
            {
                symbol = payload.Symbol,
                starting_cash = payload.StartingCash,
            }),
            StartDate = startDateStr,
            EndDate = endDateStr,
            Timespan = "minute",
            Multiplier = 1,
            TotalTrades = payload.TotalTrades,
            WinningTrades = payload.WinningTrades,
            LosingTrades = payload.LosingTrades,
            TotalPnL = payload.TotalPnl,
            InitialCash = payload.StartingCash,
            FinalEquity = payload.FinalEquity,
            TotalFees = payload.TotalFees,
            WinRate = (decimal)payload.WinRate,
            LeanStatisticsJson = payload.LeanStatistics is null
                ? null
                : JsonSerializer.Serialize(payload.LeanStatistics),
            Source = payload.Source,
            LeanRunId = payload.LeanRunId,
            FillMode = "lean-sidecar",
            ExecutedAt = DateTime.UtcNow,
            DurationMs = 0,
        };

        _db.StrategyExecutions.Add(execution);
        await _db.SaveChangesAsync(ct);

        _logger.LogInformation(
            "[STEP 3] Persisted StrategyExecution Id={Id} for LeanRunId={LeanRunId}, Trades={Count}",
            execution.Id, payload.LeanRunId, payload.Trades.Count);

        decimal cumulativePnl = 0m;
        foreach (var t in payload.Trades.OrderBy(t => t.EntryMsUtc))
        {
            cumulativePnl += t.Pnl;
            _db.BacktestTrades.Add(new BacktestTrade
            {
                StrategyExecutionId = execution.Id,
                TradeType = "LONG",
                EntryTimestamp = DateTimeOffset.FromUnixTimeMilliseconds(t.EntryMsUtc).UtcDateTime,
                ExitTimestamp = DateTimeOffset.FromUnixTimeMilliseconds(t.ExitMsUtc).UtcDateTime,
                EntryPrice = t.EntryPrice,
                ExitPrice = t.ExitPrice,
                Quantity = t.Quantity,
                PnL = t.Pnl,
                CumulativePnL = cumulativePnl,
                SignalReason = t.SignalReason,
                IsSyntheticExit = t.IsSyntheticExit,
            });
        }

        if (payload.Trades.Count > 0)
        {
            await _db.SaveChangesAsync(ct);
        }

        return execution.Id;
    }
}

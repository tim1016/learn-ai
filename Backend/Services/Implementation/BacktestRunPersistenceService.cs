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

    private static readonly HashSet<string> _allowedSources = new(StringComparer.Ordinal)
    {
        "lean-sidecar",
        "engine",
    };

    public async Task<int> PersistAsync(PersistLeanRunPayload payload, CancellationToken ct)
    {
        if (payload is null)
            throw new ArgumentNullException(nameof(payload));
        if (string.IsNullOrWhiteSpace(payload.Symbol))
            throw new ArgumentException("symbol is required", nameof(payload));
        if (payload.Trades is null)
            throw new ArgumentException("trades is required (use empty list, not null)", nameof(payload));
        if (payload.StartDateMs > payload.EndDateMs)
            throw new ArgumentException(
                $"start_date_ms ({payload.StartDateMs}) must be <= end_date_ms ({payload.EndDateMs})",
                nameof(payload));

        if (!_allowedSources.Contains(payload.Source))
        {
            throw new ArgumentException(
                $"Expected source in {{'lean-sidecar','engine'}}, got '{payload.Source}'",
                nameof(payload));
        }

        if (payload.Source == "lean-sidecar" && string.IsNullOrWhiteSpace(payload.LeanRunId))
            throw new ArgumentException("lean_run_id is required when source='lean-sidecar'", nameof(payload));

        if (payload.Source == "engine" && !string.IsNullOrWhiteSpace(payload.LeanRunId))
            throw new ArgumentException("lean_run_id must be null when source='engine'", nameof(payload));

        // Idempotency: only applies to lean-sidecar runs, where lean_run_id is the natural key.
        // Engine runs have no external idempotency key — every persist creates a new row.
        if (payload.Source == "lean-sidecar")
        {
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
            FillMode = payload.Source == "engine" ? "signal_bar_close" : "lean-sidecar",
            ExecutedAt = DateTime.UtcNow,
            DurationMs = 0,
            // PR B (2026-05-19) — DataPolicy / Commission / Brokerage.
            // When the legacy client omits the canonical block, synthesize a
            // default DataPolicy that documents what the engines actually did
            // (Polygon bars, pre-adjusted, regular session, minute-1 input
            // bars consolidated to minute-15 strategy bars). The Python
            // engine genuinely doesn't model brokerage, so falling back to
            // ``algorithm_default`` is faithful for ``source='engine'``. For
            // ``source='lean-sidecar'`` the run actually ran under SOME
            // brokerage (often Interactive Brokers for reconciliation runs);
            // if the LEAN persist payload didn't carry the field, leaving it
            // NULL ("unknown") is the truthful record. Fabricating
            // ``algorithm_default`` here would corrupt compare-view gating
            // and historical auditing, so we only synthesize that fallback
            // for the engine path.
            DataPolicyJson = payload.DataPolicyJson ?? SynthesizeLegacyDataPolicy(payload),
            CommissionPerOrder = payload.CommissionPerOrder ?? 0m,
            BrokeragePolicy = payload.BrokeragePolicy
                ?? (payload.Source == "engine" ? "algorithm_default" : null),
            RunVerdictJson = payload.RunVerdictJson,
            VerdictVersion = payload.VerdictVersion,
            VerdictGrade = payload.VerdictGrade,
            VerdictSignal = payload.VerdictSignal,
            EquityCurveJson = payload.EquityCurveJson,
            InsightSummaryJson = payload.InsightSummaryJson,
            ValidationAnalyticsJson = payload.ValidationAnalyticsJson,
            ParityGroupId = payload.ParityGroupId,
        };

        // Wrap the entire write in a transaction so a trade-save failure also rolls back
        // the StrategyExecution row (atomicity).  The FK on BacktestTrade.StrategyExecutionId
        // requires the execution to be saved first to populate its Id, so we use two
        // SaveChangesAsync calls — both inside the same transaction.
        // NOTE: InMemory EF Core does not simulate real transaction rollback, so
        // the transaction-rollback behaviour is only verified by integration tests against
        // a real Postgres instance.
        await using var tx = await _db.Database.BeginTransactionAsync(ct);

        _db.StrategyExecutions.Add(execution);
        try
        {
            await _db.SaveChangesAsync(ct);  // populates execution.Id
        }
        catch (DbUpdateException ex) when (IsUniqueViolation(ex))
        {
            // A concurrent call won the race and inserted the same LeanRunId.
            // Look up and return the existing Id so the caller is idempotent.
            var raceWinner = await _db.StrategyExecutions
                .AsNoTracking()
                .Where(s => s.Source == "lean-sidecar" && s.LeanRunId == payload.LeanRunId)
                .Select(s => s.Id)
                .FirstAsync(ct);
            return raceWinner;
        }

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

        await tx.CommitAsync(ct);
        return execution.Id;
    }

    private static bool IsUniqueViolation(DbUpdateException ex)
    {
        // Npgsql: SqlState 23505 == unique_violation
        return ex.InnerException is Npgsql.PostgresException pg && pg.SqlState == "23505";
    }

    /// <summary>
    /// PR B (2026-05-19) — one-cycle backwards-compat for pre-PR-B clients
    /// that POST without the ``data_policy_json`` field. Records what the
    /// engines actually do today (Polygon-sourced, pre-adjusted bars in
    /// regular session, minute-1 input consolidated to minute-15 strategy
    /// bars) so the history surface and compare-view never see a null
    /// DataPolicy on a freshly-written row.
    /// </summary>
    private static string SynthesizeLegacyDataPolicy(PersistLeanRunPayload p)
    {
        var dp = new
        {
            source = "polygon",
            symbol = p.Symbol?.ToUpperInvariant() ?? "",
            adjusted = true,
            session = "regular",
            input_bars = new { timespan = "minute", multiplier = 1 },
            strategy_bars = new { timespan = "minute", multiplier = 15 },
            timestamp_policy = "bar_close_ms_utc",
            timezone = "America/New_York",
            provider_kind = "live",
            fixture_id = (string?)null,
            fixture_sha256 = (string?)null,
        };
        return JsonSerializer.Serialize(dp);
    }
}

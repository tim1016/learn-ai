using System.Text.Json;
using Backend.Data;
using Backend.Models.DTOs;
using Backend.Models.MarketData;
using Backend.Services.Interfaces;
using Microsoft.EntityFrameworkCore;

namespace Backend.Services.Implementation;

/// <summary>
/// Persists one Recency Chart run snapshot atomically. Unlike
/// <see cref="BacktestRunPersistenceService"/>, this is the Recency
/// Chart's SOLE authority — every trade it writes carries a canonical
/// evidence fingerprint (design spec D16) with a database-level unique
/// constraint, so a re-run of the same combo over an overlapping window
/// never duplicates a trade row (D14/D16). A launch that has been
/// soft-deleted (its tombstone set) is honored here too: an in-flight
/// child persist call for a cancelled/deleted launch is skipped, not
/// written (P0-5 — closes the race between bulk-delete and late child
/// inserts).
/// </summary>
public class RecencyPersistenceService : IRecencyPersistenceService
{
    private readonly AppDbContext _db;
    private readonly ILogger<RecencyPersistenceService> _logger;

    public RecencyPersistenceService(AppDbContext db, ILogger<RecencyPersistenceService> logger)
    {
        _db = db;
        _logger = logger;
    }

    public async Task<RecencyPersistResult> PersistSnapshotAsync(RecencySnapshotRequest request, CancellationToken ct)
    {
        if (request is null)
            throw new ArgumentNullException(nameof(request));

        var exists = await _db.RecencyLaunches.AsNoTracking().AnyAsync(l => l.Id == request.LaunchId, ct);
        if (!exists)
        {
            throw new InvalidOperationException(
                $"RecencyLaunch '{request.LaunchId}' does not exist. Launches must be persisted before dispatch " +
                "(design spec D20) — a snapshot arriving for an unknown launch means that step was skipped.");
        }

        var runId = await InsertRunAndTradesAsync(request, ct);

        if (runId is null)
        {
            _logger.LogInformation(
                "[STEP 1] Skipping recency snapshot for tombstoned launch {LaunchId} (symbol={Symbol}, strategy={StrategyKey})",
                request.LaunchId, request.Symbol, request.StrategyKey);
            return new RecencyPersistResult(RecencyRunId: null, Skipped: true);
        }

        // Concurrent persists for the same launch are the runner's default
        // execution mode (bounded-concurrency ThreadPoolExecutor in
        // app/research/recency/runner.py), so this counter cannot be a
        // tracked read-increment-save — that loses updates under a race.
        // ExecuteUpdateAsync issues one atomic UPDATE.
        await _db.RecencyLaunches
            .Where(l => l.Id == request.LaunchId)
            .ExecuteUpdateAsync(setters => setters.SetProperty(l => l.SucceededRuns, l => l.SucceededRuns + 1), ct);

        _logger.LogInformation(
            "[STEP 2] Persisted RecencyRun Id={RunId} for launch={LaunchId} symbol={Symbol} strategy={StrategyKey} trades={Count}",
            runId, request.LaunchId, request.Symbol, request.StrategyKey, request.Trades.Count);

        return new RecencyPersistResult(RecencyRunId: runId, Skipped: false);
    }

    /// <summary>
    /// Inserts the run and its trades in one transaction, or returns null
    /// if the launch was (or became) tombstoned. The tombstone check runs
    /// INSIDE this transaction via <c>SELECT ... FOR UPDATE</c> on the
    /// launch row — a plain pre-check before opening the transaction has a
    /// window where a concurrent soft-delete can commit between the check
    /// and this insert, letting a "deleted" launch gain new children a
    /// later restore would resurrect. Row-locking the launch here means a
    /// concurrent soft-delete's own UPDATE blocks until this transaction
    /// finishes, so the two operations always serialize into one
    /// unambiguous order instead of racing.
    ///
    /// Duplicate-fingerprint trades (a re-run of the same combo over an
    /// overlapping window) don't just skip the insert — every run that
    /// produces a fingerprint records a <see cref="RecencyTradeMembership"/>
    /// row, so soft-deleting whichever run happened to insert the trade
    /// first doesn't vanish evidence a second, still-live run also
    /// vouches for (see RecencyQuery.GetRecencyTrades).
    /// </summary>
    private async Task<int?> InsertRunAndTradesAsync(RecencySnapshotRequest request, CancellationToken ct, int attempt = 0)
    {
        // NOTE: InMemory EF Core does not simulate real transaction rollback
        // or row locking — both are only verified against a real Postgres
        // instance (mirrors BacktestRunPersistenceService's documented
        // limitation).
        await using var tx = await _db.Database.BeginTransactionAsync(ct);

        // EF Core's SqlQuery<T> maps a scalar T's first column to a
        // property literally named "Value" — an unaliased column name
        // (e.g. "DeletedAtMs") fails at read time with "column s.Value
        // does not exist", not at compile time.
        var deletedAtMs = await _db.Database
            .SqlQuery<long?>($"SELECT \"DeletedAtMs\" AS \"Value\" FROM \"RecencyLaunches\" WHERE \"Id\" = {request.LaunchId} FOR UPDATE")
            .SingleAsync(ct);
        if (deletedAtMs is not null)
        {
            await tx.RollbackAsync(ct);
            return null;
        }

        var run = new RecencyRun
        {
            RecencyLaunchId = request.LaunchId,
            Symbol = request.Symbol,
            StrategyKey = request.StrategyKey,
            ParamsJson = JsonSerializer.Serialize(request.Params),
            ParamsHash = request.ParamsHash,
            TotalPnl = request.TotalPnl,
            Sharpe = request.Sharpe,
            StudyId = request.StudyId,
            CreatedAtMs = DateTimeOffset.UtcNow.ToUnixTimeMilliseconds(),
        };
        _db.RecencyRuns.Add(run);
        await _db.SaveChangesAsync(ct); // populates run.Id

        var incomingFingerprints = request.Trades.Select(t => t.Fingerprint).ToList();
        var existingTradeIdsByFingerprint = await _db.RecencyTrades
            .Where(t => incomingFingerprints.Contains(t.Fingerprint))
            .Select(t => new { t.Id, t.Fingerprint })
            .ToDictionaryAsync(t => t.Fingerprint, t => t.Id, StringComparer.Ordinal, ct);

        foreach (var trade in request.Trades)
        {
            if (existingTradeIdsByFingerprint.TryGetValue(trade.Fingerprint, out var existingTradeId))
            {
                // Identical canonical evidence already persisted under
                // another run — this run still gets a membership claim on
                // it, so either run's later soft-delete leaves the trade
                // visible as long as the other one is still live.
                _db.RecencyTradeMemberships.Add(new RecencyTradeMembership { RecencyTradeId = existingTradeId, RecencyRunId = run.Id });
                continue;
            }

            var newTrade = new RecencyTrade
            {
                RecencyRunId = run.Id,
                Fingerprint = trade.Fingerprint,
                EntryMs = trade.EntryMs,
                ExitMs = trade.ExitMs,
                PnlPts = trade.PnlPts,
                PnlPct = trade.PnlPct,
                Quantity = trade.Quantity,
                Pnl = trade.Pnl,
                HoldingSessions = trade.HoldingSessions,
                IsSyntheticExit = trade.IsSyntheticExit,
                SignalReason = trade.SignalReason,
            };
            _db.RecencyTrades.Add(newTrade);
            _db.RecencyTradeMemberships.Add(new RecencyTradeMembership { RecencyTrade = newTrade, RecencyRunId = run.Id });
        }

        try
        {
            await _db.SaveChangesAsync(ct);
        }
        catch (DbUpdateException ex) when (PostgresErrors.IsUniqueViolation(ex) && attempt < 3)
        {
            await tx.RollbackAsync(ct);
            _db.ChangeTracker.Clear();
            return await InsertRunAndTradesAsync(request, ct, attempt + 1);
        }

        await tx.CommitAsync(ct);
        return run.Id;
    }
}

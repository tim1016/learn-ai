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

        var launch = await _db.RecencyLaunches
            .AsNoTracking()
            .Where(l => l.Id == request.LaunchId)
            .Select(l => new { l.Id, l.DeletedAtMs })
            .SingleOrDefaultAsync(ct);

        if (launch is null)
        {
            throw new InvalidOperationException(
                $"RecencyLaunch '{request.LaunchId}' does not exist. Launches must be persisted before dispatch " +
                "(design spec D20) — a snapshot arriving for an unknown launch means that step was skipped.");
        }

        if (launch.DeletedAtMs is not null)
        {
            _logger.LogInformation(
                "[STEP 1] Skipping recency snapshot for tombstoned launch {LaunchId} (symbol={Symbol}, strategy={StrategyKey})",
                request.LaunchId, request.Symbol, request.StrategyKey);
            return new RecencyPersistResult(RecencyRunId: null, Skipped: true);
        }

        var runId = await InsertRunAndTradesAsync(request, ct);

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
    /// Inserts the run and its non-duplicate trades in one transaction. A
    /// concurrent writer can win the race on a shared fingerprint between
    /// this method's pre-check and its insert (two overlapping-window
    /// re-runs persisting the same canonical evidence at the same time);
    /// Postgres aborts the whole transaction on that unique violation, so a
    /// single retry re-reads what the winner just committed and inserts
    /// only what's still missing — never silently drops this snapshot's
    /// other trades over one collision.
    /// </summary>
    private async Task<int> InsertRunAndTradesAsync(RecencySnapshotRequest request, CancellationToken ct, int attempt = 0)
    {
        // NOTE: InMemory EF Core does not simulate real transaction rollback —
        // rollback behaviour is only verified against a real Postgres instance
        // (mirrors BacktestRunPersistenceService's documented limitation).
        await using var tx = await _db.Database.BeginTransactionAsync(ct);

        var run = new RecencyRun
        {
            RecencyLaunchId = request.LaunchId,
            Symbol = request.Symbol,
            StrategyKey = request.StrategyKey,
            ParamsJson = JsonSerializer.Serialize(request.Params),
            ParamsHash = request.ParamsHash,
            TotalPnl = request.TotalPnl,
            Sharpe = request.Sharpe,
            CreatedAtMs = DateTimeOffset.UtcNow.ToUnixTimeMilliseconds(),
        };
        _db.RecencyRuns.Add(run);
        await _db.SaveChangesAsync(ct); // populates run.Id

        var incomingFingerprints = request.Trades.Select(t => t.Fingerprint).ToList();
        var existingFingerprints = await _db.RecencyTrades
            .Where(t => incomingFingerprints.Contains(t.Fingerprint))
            .Select(t => t.Fingerprint)
            .ToListAsync(ct);
        var existingSet = existingFingerprints.ToHashSet(StringComparer.Ordinal);

        foreach (var trade in request.Trades)
        {
            if (existingSet.Contains(trade.Fingerprint))
                continue; // identical canonical evidence already persisted under another run

            _db.RecencyTrades.Add(new RecencyTrade
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
            });
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

using Backend.Data;
using Backend.GraphQL.Types;
using HotChocolate;
using Microsoft.EntityFrameworkCore;

namespace Backend.GraphQL;

/// <summary>
/// Reads for the Recency Chart's accumulated projection. Both resolvers
/// only filter, join, and select already-persisted Python-authored numbers
/// (AGENTS.md #5) — no PnL, Sharpe, or statistic is derived here.
/// </summary>
[ExtendObjectType(typeof(Query))]
public class RecencyQuery
{
    /// <summary>
    /// Trades overlapping [fromMs, toMs] — entered at or before toMs and
    /// exited at or after fromMs — excluding any whose run or launch has
    /// been soft-deleted. An entry-only predicate would drop a position
    /// entered just before the window that's still open inside it; the
    /// swimlane already clips overlapping spans at the window boundary
    /// on render, so the fetch must actually include them. Dedup-by-
    /// fingerprint (design spec D16) needs no query-time logic here:
    /// RecencyTrade.Fingerprint carries a database-level unique
    /// constraint, so two rows can never share one — every row already
    /// IS the canonical (and only) representative of its evidence.
    /// Liveness, though, is membership-based: a re-run of the same combo
    /// over an overlapping window produces the identical fingerprint, and
    /// each such run gets its own RecencyTradeMembership row — a trade
    /// stays visible as long as ANY claiming run (and its launch) is
    /// still live, not just the one that happened to insert the physical
    /// row first.
    /// </summary>
    [GraphQLName("recencyTrades")]
    public async Task<List<RecencyTradeType>> GetRecencyTrades(
        AppDbContext context,
        long fromMs,
        long toMs,
        List<string>? symbols,
        List<string>? strategies,
        CancellationToken cancellationToken)
    {
        var query = context.RecencyTrades
            .AsNoTracking()
            .Where(t => t.EntryMs <= toMs && t.ExitMs >= fromMs)
            .Where(t => t.Memberships.Any(m => m.RecencyRun.DeletedAtMs == null && m.RecencyRun.RecencyLaunch.DeletedAtMs == null));

        if (symbols is { Count: > 0 })
            query = query.Where(t => symbols.Contains(t.RecencyRun.Symbol));
        if (strategies is { Count: > 0 })
            query = query.Where(t => strategies.Contains(t.RecencyRun.StrategyKey));

        return await query
            .Select(t => new RecencyTradeType
            {
                Symbol = t.RecencyRun.Symbol,
                StrategyKey = t.RecencyRun.StrategyKey,
                ParamsHash = t.RecencyRun.ParamsHash,
                ParamsJson = t.RecencyRun.ParamsJson,
                Fingerprint = t.Fingerprint,
                EntryMs = t.EntryMs,
                ExitMs = t.ExitMs,
                PnlPts = t.PnlPts,
                PnlPct = t.PnlPct,
                Quantity = t.Quantity,
                Pnl = t.Pnl,
                HoldingSessions = t.HoldingSessions,
                Sharpe = t.RecencyRun.Sharpe,
                StudyId = t.RecencyRun.StudyId,
                RecencyRunId = t.RecencyRunId,
                IsSyntheticExit = t.IsSyntheticExit,
                SignalReason = t.SignalReason,
            })
            .ToListAsync(cancellationToken);
    }

    /// <summary>
    /// The highest-TotalPnl combo per (symbol, strategy) among non-deleted
    /// runs. Without a window, TotalPnl is Python's persisted all-time sum
    /// over the run's own generation window. With [fromMs, toMs] (the
    /// caller's current display window), the ranking instead sums each
    /// run's already-Python-computed per-trade Pnl for trades entering
    /// inside that window — a SQL SUM over numbers Python already
    /// computed, not a new derived statistic — so a launch spanning months
    /// can't have its all-time winner shadow the combo that's actually
    /// best in what the all-symbols recent-window view shows.
    /// </summary>
    [GraphQLName("recencyHero")]
    public async Task<List<RecencyHeroType>> GetRecencyHero(
        AppDbContext context,
        List<string>? symbols,
        List<string>? strategies,
        long? fromMs,
        long? toMs,
        CancellationToken cancellationToken)
    {
        var query = context.RecencyRuns
            .AsNoTracking()
            .Where(r => r.DeletedAtMs == null && r.RecencyLaunch.DeletedAtMs == null);

        if (symbols is { Count: > 0 })
            query = query.Where(r => symbols.Contains(r.Symbol));
        if (strategies is { Count: > 0 })
            query = query.Where(r => strategies.Contains(r.StrategyKey));

        var candidates = fromMs is null || toMs is null
            ? await query
                .Select(r => new { r.Id, r.Symbol, r.StrategyKey, r.ParamsHash, TotalPnl = r.TotalPnl })
                .ToListAsync(cancellationToken)
            : await query
                .Select(r => new
                {
                    r.Id,
                    r.Symbol,
                    r.StrategyKey,
                    r.ParamsHash,
                    TotalPnl = r.Trades
                        .Where(t => t.EntryMs <= toMs && t.ExitMs >= fromMs)
                        .Sum(t => (decimal?)t.Pnl) ?? 0m,
                })
                .ToListAsync(cancellationToken);

        // "Top 1 per group" grouped queries translate unreliably across EF
        // Core providers — grouped in memory instead, after already
        // filtering server-side. Recency's run cardinality is bounded
        // (one row per symbol x strategy x combo x launch), not a
        // millions-of-rows table.
        return candidates
            .GroupBy(r => (r.Symbol, r.StrategyKey))
            .Select(g => g.OrderByDescending(r => r.TotalPnl).First())
            .Select(r => new RecencyHeroType
            {
                Symbol = r.Symbol,
                StrategyKey = r.StrategyKey,
                ParamsHash = r.ParamsHash,
                TotalPnl = r.TotalPnl,
                RecencyRunId = r.Id,
            })
            .ToList();
    }
}

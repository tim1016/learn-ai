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
    /// Trades entering within [fromMs, toMs], excluding any whose run or
    /// launch has been soft-deleted. Dedup-by-fingerprint (design spec
    /// D16) needs no query-time logic here: RecencyTrade.Fingerprint
    /// carries a database-level unique constraint, so two rows can never
    /// share one — every row already IS the canonical (and only)
    /// representative of its evidence.
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
            .Where(t => t.EntryMs >= fromMs && t.EntryMs <= toMs)
            .Where(t => t.RecencyRun.DeletedAtMs == null && t.RecencyRun.RecencyLaunch.DeletedAtMs == null);

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
            })
            .ToListAsync(cancellationToken);
    }

    /// <summary>
    /// The highest-TotalPnl combo per (symbol, strategy) among non-deleted
    /// runs. v1 scope: TotalPnl was computed by Python over each run's own
    /// generation window at persist time — this is not yet recomputed
    /// relative to a display window the user is currently zoomed to (that
    /// lands with the display-window UI in a later slice).
    /// </summary>
    [GraphQLName("recencyHero")]
    public async Task<List<RecencyHeroType>> GetRecencyHero(
        AppDbContext context,
        List<string>? symbols,
        List<string>? strategies,
        CancellationToken cancellationToken)
    {
        var query = context.RecencyRuns
            .AsNoTracking()
            .Where(r => r.DeletedAtMs == null && r.RecencyLaunch.DeletedAtMs == null);

        if (symbols is { Count: > 0 })
            query = query.Where(r => symbols.Contains(r.Symbol));
        if (strategies is { Count: > 0 })
            query = query.Where(r => strategies.Contains(r.StrategyKey));

        var candidates = await query
            .Select(r => new { r.Id, r.Symbol, r.StrategyKey, r.ParamsHash, r.TotalPnl })
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

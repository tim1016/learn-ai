using Backend.Data;
using Backend.GraphQL.Types;
using Backend.Models.DTOs;
using Backend.Models.MarketData;
using Backend.Services.Interfaces;
using HotChocolate;
using Microsoft.EntityFrameworkCore;

namespace Backend.GraphQL;

/// <summary>
/// Reads for the Recency Chart projection. Python owns all numerical
/// derivation; this transport only filters evidence and projects it.
/// </summary>
[ExtendObjectType(typeof(Query))]
public static class RecencyQuery
{
    [GraphQLName("recencyTrades")]
    public static async Task<List<RecencyTradeType>> GetRecencyTrades(
        [Service] AppDbContext context,
        long fromMs,
        long toMs,
        List<string>? symbols,
        List<string>? strategies,
        CancellationToken cancellationToken)
    {
        var query = context.RecencyTrades
            .AsNoTracking()
            .Where(t => t.EntryMs <= toMs && t.ExitMs >= fromMs)
            .Where(t => t.Memberships.Any(m =>
                m.RecencyRun.DeletedAtMs == null
                && m.RecencyRun.RecencyLaunch.DeletedAtMs == null
                && (symbols == null || symbols.Count == 0 || symbols.Contains(m.RecencyRun.Symbol))
                && (strategies == null || strategies.Count == 0 || strategies.Contains(m.RecencyRun.StrategyKey))))
            .Include(t => t.Memberships)
                .ThenInclude(m => m.RecencyRun)
                .ThenInclude(r => r.RecencyLaunch);

        var trades = await query.ToListAsync(cancellationToken);
        var result = new List<RecencyTradeType>(trades.Count);
        foreach (var trade in trades)
        {
            var liveMemberships = trade.Memberships
                .Where(IsLive)
                .OrderByDescending(m => m.RecencyRun.CreatedAtMs)
                .ThenByDescending(m => m.RecencyRunId)
                .ToList();
            var representative = liveMemberships.First(m =>
                (symbols is not { Count: > 0 } || symbols.Contains(m.RecencyRun.Symbol))
                && (strategies is not { Count: > 0 } || strategies.Contains(m.RecencyRun.StrategyKey)));

            result.Add(new RecencyTradeType
            {
                Symbol = representative.RecencyRun.Symbol,
                StrategyKey = representative.RecencyRun.StrategyKey,
                ParamsHash = representative.RecencyRun.ParamsHash,
                ParamsJson = representative.RecencyRun.ParamsJson,
                Fingerprint = trade.Fingerprint,
                EntryMs = trade.EntryMs,
                ExitMs = trade.ExitMs,
                PnlPts = trade.PnlPts,
                PnlPct = trade.PnlPct,
                Quantity = trade.Quantity,
                Pnl = trade.Pnl,
                HoldingSessions = trade.HoldingSessions,
                Sharpe = representative.RecencyRun.Sharpe,
                StudyId = representative.RecencyRun.StudyId,
                RecencyRunId = representative.RecencyRunId,
                IsSyntheticExit = trade.IsSyntheticExit,
                SignalReason = trade.SignalReason,
                Memberships = liveMemberships.Select(m => new RecencyTradeMembershipType
                {
                    RecencyRunId = m.RecencyRunId,
                    StudyId = m.RecencyRun.StudyId,
                    CreatedAtMs = m.RecencyRun.CreatedAtMs,
                }).ToList(),
            });
        }
        return result;
    }

    [GraphQLName("recencyHero")]
    public static async Task<List<RecencyHeroType>> GetRecencyHero(
        [Service] AppDbContext context,
        [Service] IRecencyHeroClient heroClient,
        List<string>? symbols,
        List<string>? strategies,
        long fromMs,
        long toMs,
        CancellationToken cancellationToken)
    {
        if (fromMs > toMs)
            throw new ArgumentException("fromMs must be less than or equal to toMs");

        var query = context.RecencyTradeMemberships
            .AsNoTracking()
            .Where(m => m.RecencyRun.DeletedAtMs == null && m.RecencyRun.RecencyLaunch.DeletedAtMs == null)
            .Where(m => m.RecencyTrade.EntryMs >= fromMs && m.RecencyTrade.EntryMs <= toMs);
        if (symbols is { Count: > 0 })
            query = query.Where(m => symbols.Contains(m.RecencyRun.Symbol));
        if (strategies is { Count: > 0 })
            query = query.Where(m => strategies.Contains(m.RecencyRun.StrategyKey));

        var rows = await query.Select(m => new
        {
            m.RecencyRunId,
            m.RecencyRun.Symbol,
            m.RecencyRun.StrategyKey,
            m.RecencyRun.ParamsHash,
            m.RecencyTrade.EntryMs,
            m.RecencyTrade.Pnl,
        }).ToListAsync(cancellationToken);

        var candidates = rows
            .GroupBy(row => new { row.RecencyRunId, row.Symbol, row.StrategyKey, row.ParamsHash })
            .Select(group => new RecencyHeroCandidateRequest(
                group.Key.RecencyRunId,
                group.Key.Symbol,
                group.Key.StrategyKey,
                group.Key.ParamsHash,
                group.Select(row => new RecencyHeroTradeRequest(row.EntryMs, row.Pnl)).ToList()))
            .ToList();
        if (candidates.Count == 0)
            return [];

        var heroes = await heroClient.SelectHeroesAsync(
            new RecencyHeroRequest(fromMs, toMs, candidates),
            cancellationToken);
        return heroes.Select(hero => new RecencyHeroType
        {
            Symbol = hero.Symbol,
            StrategyKey = hero.StrategyKey,
            ParamsHash = hero.ParamsHash,
            TotalPnl = hero.TotalPnl,
            RecencyRunId = hero.RecencyRunId,
        }).ToList();
    }

    private static bool IsLive(RecencyTradeMembership membership) =>
        membership.RecencyRun.DeletedAtMs is null && membership.RecencyRun.RecencyLaunch.DeletedAtMs is null;
}

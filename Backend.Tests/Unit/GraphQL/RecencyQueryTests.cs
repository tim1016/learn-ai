using Backend.Data;
using Backend.GraphQL;
using Backend.Models.DTOs;
using Backend.Models.MarketData;
using Backend.Services.Interfaces;
using Backend.Tests.Helpers;
using Microsoft.EntityFrameworkCore;

namespace Backend.Tests.Unit.GraphQL;

/// <summary>
/// Tests the recency resolvers' query/dedup/grouping logic directly against
/// an InMemory AppDbContext, mirroring BacktestRunResolverTests' precedent
/// of testing resolver methods at the schema-neutral level — this repo has
/// no existing IRequestExecutor test-schema harness to build against, and
/// the logic under test (filtering, tombstone-exclusion, top-1-per-group)
/// lives entirely in the LINQ query, not in HotChocolate wiring.
/// </summary>
public class RecencyQueryTests
{
    private sealed class PythonHeroStub : IRecencyHeroClient
    {
        public Task<IReadOnlyList<RecencyHeroResponseItem>> SelectHeroesAsync(
            RecencyHeroRequest request,
            CancellationToken ct)
        {
            IReadOnlyList<RecencyHeroResponseItem> result = request.Candidates
                .GroupBy(candidate => (candidate.Symbol, candidate.StrategyKey))
                .Select(group => group
                    .Select(candidate => new RecencyHeroResponseItem(
                        candidate.RecencyRunId,
                        candidate.Symbol,
                        candidate.StrategyKey,
                        candidate.ParamsHash,
                        candidate.Trades.Sum(trade => trade.Pnl)))
                    .OrderByDescending(candidate => candidate.TotalPnl)
                    .First())
                .ToList();
            return Task.FromResult(result);
        }
    }

    private static readonly IRecencyHeroClient HeroClient = new PythonHeroStub();

    private static async Task<RecencyLaunch> SeedLaunchAsync(AppDbContext db, string id, long? deletedAtMs = null)
    {
        var launch = new RecencyLaunch { Id = id, ConfigJson = "{}", Status = "RUNNING", CreatedAtMs = 1, DeletedAtMs = deletedAtMs };
        db.RecencyLaunches.Add(launch);
        await db.SaveChangesAsync();
        return launch;
    }

    private static async Task<RecencyRun> SeedRunAsync(
        AppDbContext db,
        string launchId,
        string symbol,
        string strategyKey,
        string paramsHash,
        decimal totalPnl,
        decimal? sharpe = null,
        long? deletedAtMs = null)
    {
        var run = new RecencyRun
        {
            RecencyLaunchId = launchId,
            Symbol = symbol,
            StrategyKey = strategyKey,
            ParamsJson = "{}",
            ParamsHash = paramsHash,
            TotalPnl = totalPnl,
            Sharpe = sharpe,
            CreatedAtMs = 1,
            DeletedAtMs = deletedAtMs,
        };
        db.RecencyRuns.Add(run);
        await db.SaveChangesAsync();
        return run;
    }

    private static async Task SeedTradeAsync(
        AppDbContext db,
        int runId,
        string fingerprint,
        long entryMs,
        long exitMs,
        decimal pnl = 10m)
    {
        var trade = new RecencyTrade
        {
            RecencyRunId = runId,
            Fingerprint = fingerprint,
            EntryMs = entryMs,
            ExitMs = exitMs,
            PnlPts = 1m,
            PnlPct = 0.01m,
            Quantity = 10m,
            Pnl = pnl,
            HoldingSessions = 1,
        };
        db.RecencyTrades.Add(trade);
        await db.SaveChangesAsync();
        // Liveness (GetRecencyTrades) is membership-based, not just the
        // trade's single owning-run FK — see RecencyTradeMembership.
        db.RecencyTradeMemberships.Add(new RecencyTradeMembership { RecencyTradeId = trade.Id, RecencyRunId = runId });
        await db.SaveChangesAsync();
    }

    [Fact]
    public async Task GetRecencyTrades_ReturnsTradesWithinTheWindow()
    {
        var db = TestDbContextFactory.Create();
        await SeedLaunchAsync(db, "l1");
        var run = await SeedRunAsync(db, "l1", "SPY", "ema_crossover_2_bps", "hash1", 20m);
        await SeedTradeAsync(db, run.Id, "fp-in", entryMs: 500, exitMs: 600);
        await SeedTradeAsync(db, run.Id, "fp-before", entryMs: 50, exitMs: 60);
        await SeedTradeAsync(db, run.Id, "fp-after", entryMs: 5000, exitMs: 5100);

        var result = await RecencyQuery.GetRecencyTrades(db, fromMs: 100, toMs: 1000, symbols: null, strategies: null, CancellationToken.None);

        Assert.Single(result);
        Assert.Equal("fp-in", result[0].Fingerprint);
    }

    [Fact]
    public async Task GetRecencyTrades_ProjectsSyntheticExitProvenance()
    {
        var db = TestDbContextFactory.Create();
        await SeedLaunchAsync(db, "l1");
        var run = await SeedRunAsync(db, "l1", "SPY", "ema_crossover_2_bps", "hash1", 20m);
        var trade = new RecencyTrade
        {
            RecencyRunId = run.Id,
            Fingerprint = "fp-synthetic",
            EntryMs = 500,
            ExitMs = 600,
            PnlPts = 1m,
            PnlPct = 0.01m,
            Quantity = 10m,
            Pnl = 10m,
            HoldingSessions = 1,
            IsSyntheticExit = true,
            SignalReason = "window_close",
        };
        db.RecencyTrades.Add(trade);
        await db.SaveChangesAsync();
        db.RecencyTradeMemberships.Add(new RecencyTradeMembership { RecencyTradeId = trade.Id, RecencyRunId = run.Id });
        await db.SaveChangesAsync();

        var result = await RecencyQuery.GetRecencyTrades(db, fromMs: 100, toMs: 1000, symbols: null, strategies: null, CancellationToken.None);

        Assert.Single(result);
        Assert.True(result[0].IsSyntheticExit);
        Assert.Equal("window_close", result[0].SignalReason);
    }

    [Fact]
    public async Task GetRecencyTrades_IncludesAPositionEnteredBeforeTheWindowButStillOpenInsideIt()
    {
        var db = TestDbContextFactory.Create();
        await SeedLaunchAsync(db, "l1");
        var run = await SeedRunAsync(db, "l1", "SPY", "ema_crossover_2_bps", "hash1", 20m);
        // Entered before fromMs=100, but exits at 200 — inside the window.
        // An entry-only predicate would drop this even though the trade
        // genuinely overlaps [100, 1000].
        await SeedTradeAsync(db, run.Id, "fp-open-before-window", entryMs: 50, exitMs: 200);

        var result = await RecencyQuery.GetRecencyTrades(db, fromMs: 100, toMs: 1000, symbols: null, strategies: null, CancellationToken.None);

        Assert.Single(result);
        Assert.Equal("fp-open-before-window", result[0].Fingerprint);
    }

    [Fact]
    public async Task GetRecencyTrades_ExcludesTradesFromSoftDeletedRuns()
    {
        var db = TestDbContextFactory.Create();
        await SeedLaunchAsync(db, "l1");
        var deletedRun = await SeedRunAsync(db, "l1", "SPY", "ema_crossover_2_bps", "hash1", 20m, deletedAtMs: 999);
        var liveRun = await SeedRunAsync(db, "l1", "SPY", "ema_crossover_2_bps", "hash2", 30m);
        await SeedTradeAsync(db, deletedRun.Id, "fp-deleted", 100, 200);
        await SeedTradeAsync(db, liveRun.Id, "fp-live", 100, 200);

        var result = await RecencyQuery.GetRecencyTrades(db, fromMs: 0, toMs: 1000, symbols: null, strategies: null, CancellationToken.None);

        Assert.Single(result);
        Assert.Equal("fp-live", result[0].Fingerprint);
    }

    [Fact]
    public async Task GetRecencyTrades_ExcludesTradesFromSoftDeletedLaunches()
    {
        var db = TestDbContextFactory.Create();
        await SeedLaunchAsync(db, "l1", deletedAtMs: 999);
        var run = await SeedRunAsync(db, "l1", "SPY", "ema_crossover_2_bps", "hash1", 20m);
        await SeedTradeAsync(db, run.Id, "fp1", 100, 200);

        var result = await RecencyQuery.GetRecencyTrades(db, fromMs: 0, toMs: 1000, symbols: null, strategies: null, CancellationToken.None);

        Assert.Empty(result);
    }

    [Fact]
    public async Task GetRecencyTrades_FiltersBySymbolAndStrategy()
    {
        var db = TestDbContextFactory.Create();
        await SeedLaunchAsync(db, "l1");
        var spyRun = await SeedRunAsync(db, "l1", "SPY", "ema_crossover_2_bps", "h1", 20m);
        var aaplRun = await SeedRunAsync(db, "l1", "AAPL", "rsi_mean_reversion", "h2", 10m);
        await SeedTradeAsync(db, spyRun.Id, "fp-spy", 100, 200);
        await SeedTradeAsync(db, aaplRun.Id, "fp-aapl", 100, 200);

        var result = await RecencyQuery.GetRecencyTrades(db, 0, 1000, symbols: new List<string> { "SPY" }, strategies: null, CancellationToken.None);

        Assert.Single(result);
        Assert.Equal("fp-spy", result[0].Fingerprint);
    }

    [Fact]
    public async Task GetRecencyTrades_CarriesOwningRunsSharpeAndStudyId()
    {
        var db = TestDbContextFactory.Create();
        await SeedLaunchAsync(db, "l1");
        var run = await SeedRunAsync(db, "l1", "SPY", "ema_crossover_2_bps", "h1", 20m, sharpe: 1.5m);
        run.StudyId = 42;
        await db.SaveChangesAsync();
        await SeedTradeAsync(db, run.Id, "fp1", 100, 200);

        var result = await RecencyQuery.GetRecencyTrades(db, 0, 1000, null, null, CancellationToken.None);

        Assert.Equal(1.5m, result[0].Sharpe);
        Assert.Equal(42, result[0].StudyId);
        Assert.Equal(run.Id, result[0].RecencyRunId);
    }

    [Fact]
    public async Task GetRecencyTrades_CarriesOwningRunsParamsJson()
    {
        var db = TestDbContextFactory.Create();
        await SeedLaunchAsync(db, "l1");
        var run = await SeedRunAsync(db, "l1", "SPY", "ema_crossover_2_bps", "h1", 20m);
        run.ParamsJson = "{\"gap_bps\":2.0,\"rsi_min\":50.0}";
        await db.SaveChangesAsync();
        await SeedTradeAsync(db, run.Id, "fp1", 100, 200);

        var result = await RecencyQuery.GetRecencyTrades(db, 0, 1000, null, null, CancellationToken.None);

        Assert.Equal("{\"gap_bps\":2.0,\"rsi_min\":50.0}", result[0].ParamsJson);
    }

    [Fact]
    public async Task GetRecencyTrades_UsesLatestLiveRepresentativeAndReturnsAllMemberships()
    {
        var db = TestDbContextFactory.Create();
        await SeedLaunchAsync(db, "l1");
        var original = await SeedRunAsync(db, "l1", "SPY", "ema_crossover_2_bps", "old", 10m);
        original.CreatedAtMs = 10;
        var latest = await SeedRunAsync(db, "l1", "SPY", "ema_crossover_2_bps", "new", 10m);
        latest.CreatedAtMs = 20;
        await db.SaveChangesAsync();
        await SeedTradeAsync(db, original.Id, "shared-fp", 100, 200);
        var trade = await db.RecencyTrades.SingleAsync(t => t.Fingerprint == "shared-fp");
        db.RecencyTradeMemberships.Add(new RecencyTradeMembership
        {
            RecencyTradeId = trade.Id,
            RecencyRunId = latest.Id,
        });
        await db.SaveChangesAsync();

        original.DeletedAtMs = 999;
        await db.SaveChangesAsync();

        var result = await RecencyQuery.GetRecencyTrades(db, 0, 1000, null, null, CancellationToken.None);

        Assert.Single(result);
        Assert.Equal(latest.Id, result[0].RecencyRunId);
        Assert.Equal("new", result[0].ParamsHash);
        Assert.Single(result[0].Memberships);
        Assert.Equal(latest.Id, result[0].Memberships[0].RecencyRunId);
    }

    [Fact]
    public async Task GetRecencyHero_PicksHighestTotalPnlPerSymbolAndStrategy()
    {
        var db = TestDbContextFactory.Create();
        await SeedLaunchAsync(db, "l1");
        var low = await SeedRunAsync(db, "l1", "SPY", "ema_crossover_2_bps", "h-low", totalPnl: 5m);
        var best = await SeedRunAsync(db, "l1", "SPY", "ema_crossover_2_bps", "h-high", totalPnl: 50m);
        var other = await SeedRunAsync(db, "l1", "AAPL", "ema_crossover_2_bps", "h-other-symbol", totalPnl: 999m);
        await SeedTradeAsync(db, low.Id, "fp-low", 100, 200, 5m);
        await SeedTradeAsync(db, best.Id, "fp-high", 100, 200, 50m);
        await SeedTradeAsync(db, other.Id, "fp-other", 100, 200, 999m);

        var result = await RecencyQuery.GetRecencyHero(db, HeroClient, symbols: new List<string> { "SPY" }, strategies: null, fromMs: 0, toMs: 1000, CancellationToken.None);

        Assert.Single(result);
        Assert.Equal("h-high", result[0].ParamsHash);
        Assert.Equal(50m, result[0].TotalPnl);
        Assert.Equal(best.Id, result[0].RecencyRunId);
    }

    [Fact]
    public async Task GetRecencyHero_ExcludesSoftDeletedRuns()
    {
        var db = TestDbContextFactory.Create();
        await SeedLaunchAsync(db, "l1");
        var deleted = await SeedRunAsync(db, "l1", "SPY", "ema_crossover_2_bps", "h-deleted-best", totalPnl: 999m, deletedAtMs: 1);
        var survivor = await SeedRunAsync(db, "l1", "SPY", "ema_crossover_2_bps", "h-survivor", totalPnl: 10m);
        await SeedTradeAsync(db, deleted.Id, "fp-deleted", 100, 200, 999m);
        await SeedTradeAsync(db, survivor.Id, "fp-survivor", 100, 200, 10m);

        var result = await RecencyQuery.GetRecencyHero(db, HeroClient, null, null, fromMs: 0, toMs: 1000, CancellationToken.None);

        Assert.Single(result);
        Assert.Equal(survivor.Id, result[0].RecencyRunId);
    }

    [Fact]
    public async Task GetRecencyHero_ExcludesRunsWithoutEntriesInTheWindow()
    {
        var db = TestDbContextFactory.Create();
        await SeedLaunchAsync(db, "l1");
        var outside = await SeedRunAsync(db, "l1", "SPY", "ema_crossover_2_bps", "outside", totalPnl: 500m);
        var inside = await SeedRunAsync(db, "l1", "SPY", "ema_crossover_2_bps", "inside", totalPnl: 1m);
        await SeedTradeAsync(db, outside.Id, "fp-outside", entryMs: 100, exitMs: 200, pnl: 500m);
        await SeedTradeAsync(db, inside.Id, "fp-inside", entryMs: 500, exitMs: 600, pnl: 1m);

        var result = await RecencyQuery.GetRecencyHero(db, HeroClient, symbols: null, strategies: null, fromMs: 400, toMs: 700, CancellationToken.None);

        Assert.Single(result);
        Assert.Equal("inside", result[0].ParamsHash);
    }

    [Fact]
    public async Task GetRecencyHero_WithAWindow_RanksByTradesEnteringInsideItNotTheRunsAllTimeTotal()
    {
        var db = TestDbContextFactory.Create();
        await SeedLaunchAsync(db, "l1");
        // Run A: huge all-time TotalPnl (e.g. from a 2-year generation
        // window), but its only trade actually inside the display window
        // is a small loser.
        var runA = await SeedRunAsync(db, "l1", "SPY", "ema_crossover_2_bps", "h-alltime-winner", totalPnl: 500m);
        db.RecencyTrades.Add(new RecencyTrade
        {
            RecencyRunId = runA.Id,
            Fingerprint = "fp-a-in-window",
            EntryMs = 500,
            ExitMs = 600,
            PnlPts = -1m,
            PnlPct = -0.01m,
            Quantity = 10m,
            Pnl = -10m,
            HoldingSessions = 1,
        });
        // Run B: modest all-time TotalPnl, but its trade inside the
        // display window is the actual best performer right now.
        var runB = await SeedRunAsync(db, "l1", "SPY", "ema_crossover_2_bps", "h-window-winner", totalPnl: 50m);
        db.RecencyTrades.Add(new RecencyTrade
        {
            RecencyRunId = runB.Id,
            Fingerprint = "fp-b-in-window",
            EntryMs = 500,
            ExitMs = 600,
            PnlPts = 20m,
            PnlPct = 0.2m,
            Quantity = 10m,
            Pnl = 200m,
            HoldingSessions = 1,
        });
        await db.SaveChangesAsync();
        var windowTrades = await db.RecencyTrades.Where(t => t.RecencyRunId == runA.Id || t.RecencyRunId == runB.Id).ToListAsync();
        db.RecencyTradeMemberships.AddRange(windowTrades.Select(t => new RecencyTradeMembership
        {
            RecencyTradeId = t.Id,
            RecencyRunId = t.RecencyRunId,
        }));
        await db.SaveChangesAsync();

        var result = await RecencyQuery.GetRecencyHero(db, HeroClient, symbols: null, strategies: null, fromMs: 100, toMs: 1000, CancellationToken.None);

        Assert.Single(result);
        Assert.Equal("h-window-winner", result[0].ParamsHash);
        Assert.Equal(200m, result[0].TotalPnl);
    }

    [Fact]
    public async Task GetRecencyHero_ReturnsOneRowPerDistinctSymbolStrategyPair()
    {
        var db = TestDbContextFactory.Create();
        await SeedLaunchAsync(db, "l1");
        var first = await SeedRunAsync(db, "l1", "SPY", "ema_crossover_2_bps", "h1", 10m);
        var second = await SeedRunAsync(db, "l1", "SPY", "rsi_mean_reversion", "h2", 10m);
        var third = await SeedRunAsync(db, "l1", "AAPL", "ema_crossover_2_bps", "h3", 10m);
        await SeedTradeAsync(db, first.Id, "fp-1", 100, 200);
        await SeedTradeAsync(db, second.Id, "fp-2", 100, 200);
        await SeedTradeAsync(db, third.Id, "fp-3", 100, 200);

        var result = await RecencyQuery.GetRecencyHero(db, HeroClient, null, null, fromMs: 0, toMs: 1000, CancellationToken.None);

        Assert.Equal(3, result.Count);
    }
}

using Backend.Data;
using Backend.GraphQL;
using Backend.Models.MarketData;
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

    private static async Task SeedTradeAsync(AppDbContext db, int runId, string fingerprint, long entryMs, long exitMs)
    {
        db.RecencyTrades.Add(new RecencyTrade
        {
            RecencyRunId = runId,
            Fingerprint = fingerprint,
            EntryMs = entryMs,
            ExitMs = exitMs,
            PnlPts = 1m,
            PnlPct = 0.01m,
            Quantity = 10m,
            Pnl = 10m,
            HoldingSessions = 1,
        });
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

        var query = new RecencyQuery();
        var result = await query.GetRecencyTrades(db, fromMs: 100, toMs: 1000, symbols: null, strategies: null, CancellationToken.None);

        Assert.Single(result);
        Assert.Equal("fp-in", result[0].Fingerprint);
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

        var query = new RecencyQuery();
        var result = await query.GetRecencyTrades(db, fromMs: 0, toMs: 1000, symbols: null, strategies: null, CancellationToken.None);

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

        var query = new RecencyQuery();
        var result = await query.GetRecencyTrades(db, fromMs: 0, toMs: 1000, symbols: null, strategies: null, CancellationToken.None);

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

        var query = new RecencyQuery();
        var result = await query.GetRecencyTrades(db, 0, 1000, symbols: new List<string> { "SPY" }, strategies: null, CancellationToken.None);

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

        var query = new RecencyQuery();
        var result = await query.GetRecencyTrades(db, 0, 1000, null, null, CancellationToken.None);

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

        var query = new RecencyQuery();
        var result = await query.GetRecencyTrades(db, 0, 1000, null, null, CancellationToken.None);

        Assert.Equal("{\"gap_bps\":2.0,\"rsi_min\":50.0}", result[0].ParamsJson);
    }

    [Fact]
    public async Task GetRecencyHero_PicksHighestTotalPnlPerSymbolAndStrategy()
    {
        var db = TestDbContextFactory.Create();
        await SeedLaunchAsync(db, "l1");
        await SeedRunAsync(db, "l1", "SPY", "ema_crossover_2_bps", "h-low", totalPnl: 5m);
        var best = await SeedRunAsync(db, "l1", "SPY", "ema_crossover_2_bps", "h-high", totalPnl: 50m);
        await SeedRunAsync(db, "l1", "AAPL", "ema_crossover_2_bps", "h-other-symbol", totalPnl: 999m);

        var query = new RecencyQuery();
        var result = await query.GetRecencyHero(db, symbols: new List<string> { "SPY" }, strategies: null, CancellationToken.None);

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
        await SeedRunAsync(db, "l1", "SPY", "ema_crossover_2_bps", "h-deleted-best", totalPnl: 999m, deletedAtMs: 1);
        var survivor = await SeedRunAsync(db, "l1", "SPY", "ema_crossover_2_bps", "h-survivor", totalPnl: 10m);

        var query = new RecencyQuery();
        var result = await query.GetRecencyHero(db, null, null, CancellationToken.None);

        Assert.Single(result);
        Assert.Equal(survivor.Id, result[0].RecencyRunId);
    }

    [Fact]
    public async Task GetRecencyHero_ReturnsOneRowPerDistinctSymbolStrategyPair()
    {
        var db = TestDbContextFactory.Create();
        await SeedLaunchAsync(db, "l1");
        await SeedRunAsync(db, "l1", "SPY", "ema_crossover_2_bps", "h1", 10m);
        await SeedRunAsync(db, "l1", "SPY", "rsi_mean_reversion", "h2", 10m);
        await SeedRunAsync(db, "l1", "AAPL", "ema_crossover_2_bps", "h3", 10m);

        var query = new RecencyQuery();
        var result = await query.GetRecencyHero(db, null, null, CancellationToken.None);

        Assert.Equal(3, result.Count);
    }
}

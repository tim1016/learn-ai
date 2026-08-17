using Backend.Data;
using Backend.GraphQL;
using Backend.Models.DTOs;
using Backend.Models.MarketData;
using Backend.Services.Implementation;
using Backend.Tests.Helpers;
using Microsoft.EntityFrameworkCore;
using Microsoft.Extensions.Logging.Abstractions;

namespace Backend.Tests.Integration;

/// <summary>
/// Postgres-backed (not InMemory): the launch-counter increment is a
/// single atomic <c>ExecuteUpdateAsync</c> UPDATE, and the fingerprint
/// insert retries through a real unique-constraint violation — both are
/// relational-provider-only operations InMemory EF Core cannot execute at
/// all (not merely simulate loosely), so every test in this class needs a
/// real connection. Concurrency is the actual execution mode: the Python
/// runner (app/research/recency/runner.py) dispatches persist calls from a
/// bounded thread pool, so concurrent snapshots for one launch are the
/// default case, not an edge case. Skips when
/// BACKEND_TEST_POSTGRES_CONNECTION_STRING is unset, matching every other
/// Postgres integration test in this suite.
/// </summary>
public class RecencyPersistenceServiceTests
{
    private static async Task<AppDbContext> OpenContextAsync(string connectionString)
    {
        var options = new DbContextOptionsBuilder<AppDbContext>().UseNpgsql(connectionString).Options;
        var db = new AppDbContext(options);
        await db.Database.OpenConnectionAsync();
        return db;
    }

    private static async Task SeedLaunchAsync(AppDbContext db, string launchId, long? deletedAtMs = null)
    {
        db.RecencyLaunches.Add(new RecencyLaunch
        {
            Id = launchId,
            ConfigJson = "{}",
            Status = "RUNNING",
            CreatedAtMs = 1_000,
            DeletedAtMs = deletedAtMs,
        });
        await db.SaveChangesAsync();
    }

    private static RecencySnapshotRequest BuildRequest(
        string launchId,
        string symbol = "SPY",
        string strategyKey = "ema_crossover_2_bps",
        IReadOnlyList<RecencyTradeSnapshotRequest>? trades = null)
    {
        var tradeList = trades ?? new[]
        {
            new RecencyTradeSnapshotRequest(
                Fingerprint: "fp-1",
                EntryMs: 100,
                ExitMs: 200,
                PnlPts: 2.0m,
                PnlPct: 0.02m,
                Quantity: 10m,
                Pnl: 20.0m,
                HoldingSessions: 1),
        };
        return new RecencySnapshotRequest(
            LaunchId: launchId,
            Symbol: symbol,
            StrategyKey: strategyKey,
            Params: new Dictionary<string, double> { ["gap_bps"] = 2.0 },
            ParamsHash: "hash-1",
            TotalPnl: 20.0m,
            Sharpe: null,
            Trades: tradeList);
    }

    [Fact]
    public async Task PersistSnapshotAsync_NewSnapshot_InsertsRunAndTrades()
    {
        await using var database = await PostgresIntegrationTestDatabase.CreateMigratedAsync();
        await using var db = await OpenContextAsync(database.ConnectionString);
        var service = new RecencyPersistenceService(db, NullLogger<RecencyPersistenceService>.Instance);
        await SeedLaunchAsync(db, "launch-1");

        var result = await service.PersistSnapshotAsync(BuildRequest("launch-1"), CancellationToken.None);

        Assert.False(result.Skipped);
        Assert.NotNull(result.RecencyRunId);

        var run = await db.RecencyRuns.Include(r => r.Trades).SingleAsync(r => r.Id == result.RecencyRunId);
        Assert.Equal("SPY", run.Symbol);
        Assert.Equal("ema_crossover_2_bps", run.StrategyKey);
        Assert.Equal("hash-1", run.ParamsHash);
        Assert.Equal(20.0m, run.TotalPnl);
        Assert.Single(run.Trades);
        Assert.Equal("fp-1", run.Trades.First().Fingerprint);
    }

    [Fact]
    public async Task PersistSnapshotAsync_NewSnapshot_PersistsStudyIdAndSyntheticExitProvenance()
    {
        await using var database = await PostgresIntegrationTestDatabase.CreateMigratedAsync();
        await using var db = await OpenContextAsync(database.ConnectionString);
        var service = new RecencyPersistenceService(db, NullLogger<RecencyPersistenceService>.Instance);
        await SeedLaunchAsync(db, "launch-1");

        var request = BuildRequest(
            "launch-1",
            trades: new[]
            {
                new RecencyTradeSnapshotRequest(
                    Fingerprint: "fp-synthetic",
                    EntryMs: 100,
                    ExitMs: 200,
                    PnlPts: 2.0m,
                    PnlPct: 0.02m,
                    Quantity: 10m,
                    Pnl: 20.0m,
                    HoldingSessions: 1,
                    IsSyntheticExit: true,
                    SignalReason: "window_close"),
            }) with
        { StudyId = 42 };

        var result = await service.PersistSnapshotAsync(request, CancellationToken.None);

        var run = await db.RecencyRuns.Include(r => r.Trades).SingleAsync(r => r.Id == result.RecencyRunId);
        Assert.Equal(42, run.StudyId);
        var trade = run.Trades.Single();
        Assert.True(trade.IsSyntheticExit);
        Assert.Equal("window_close", trade.SignalReason);
    }

    [Fact]
    public async Task PersistSnapshotAsync_NewSnapshot_IncrementsLaunchSucceededRuns()
    {
        await using var database = await PostgresIntegrationTestDatabase.CreateMigratedAsync();
        await using var db = await OpenContextAsync(database.ConnectionString);
        var service = new RecencyPersistenceService(db, NullLogger<RecencyPersistenceService>.Instance);
        await SeedLaunchAsync(db, "launch-1");

        await service.PersistSnapshotAsync(BuildRequest("launch-1"), CancellationToken.None);

        // AsNoTracking: ExecuteUpdateAsync bypasses the change tracker, so a
        // tracked read of the entity seeded above would return its stale
        // in-memory SucceededRuns rather than what's actually in the row.
        var launch = await db.RecencyLaunches.AsNoTracking().SingleAsync(l => l.Id == "launch-1");
        Assert.Equal(1, launch.SucceededRuns);
    }

    [Fact]
    public async Task PersistSnapshotAsync_ThreeSnapshotsForSameLaunch_SucceededRunsReachesThree()
    {
        await using var database = await PostgresIntegrationTestDatabase.CreateMigratedAsync();
        await using var db = await OpenContextAsync(database.ConnectionString);
        var service = new RecencyPersistenceService(db, NullLogger<RecencyPersistenceService>.Instance);
        await SeedLaunchAsync(db, "launch-1");

        await service.PersistSnapshotAsync(BuildRequest("launch-1", symbol: "SPY", trades: new[]
        {
            new RecencyTradeSnapshotRequest("fp-a", 100, 200, 1m, 0.01m, 10m, 10m, 1),
        }), CancellationToken.None);
        await service.PersistSnapshotAsync(BuildRequest("launch-1", symbol: "AAPL", trades: new[]
        {
            new RecencyTradeSnapshotRequest("fp-b", 100, 200, 1m, 0.01m, 10m, 10m, 1),
        }), CancellationToken.None);
        await service.PersistSnapshotAsync(BuildRequest("launch-1", symbol: "QQQ", trades: new[]
        {
            new RecencyTradeSnapshotRequest("fp-c", 100, 200, 1m, 0.01m, 10m, 10m, 1),
        }), CancellationToken.None);

        var launch = await db.RecencyLaunches.AsNoTracking().SingleAsync(l => l.Id == "launch-1");
        Assert.Equal(3, launch.SucceededRuns);
    }

    [Fact]
    public async Task PersistSnapshotAsync_DuplicateFingerprintAcrossTwoRuns_DoesNotDuplicateTheTradeRow()
    {
        await using var database = await PostgresIntegrationTestDatabase.CreateMigratedAsync();
        await using var db = await OpenContextAsync(database.ConnectionString);
        var service = new RecencyPersistenceService(db, NullLogger<RecencyPersistenceService>.Instance);
        await SeedLaunchAsync(db, "launch-1");

        var sharedTrade = new RecencyTradeSnapshotRequest("fp-shared", 100, 200, 2m, 0.02m, 10m, 20m, 1);
        await service.PersistSnapshotAsync(BuildRequest("launch-1", trades: new[] { sharedTrade }), CancellationToken.None);
        var second = await service.PersistSnapshotAsync(BuildRequest("launch-1", trades: new[] { sharedTrade }), CancellationToken.None);

        // Both runs are created (they're independent executions)...
        Assert.False(second.Skipped);
        Assert.Equal(2, await db.RecencyRuns.CountAsync());
        // ...but the trade evidence never duplicates.
        var persistedTrade = await db.RecencyTrades.SingleAsync(t => t.Fingerprint == "fp-shared");
        // ...and BOTH runs get a membership claim on that one physical row —
        // this is what lets either run's later soft-delete leave the trade
        // visible as long as the other is still live.
        Assert.Equal(2, await db.RecencyTradeMemberships.CountAsync(m => m.RecencyTradeId == persistedTrade.Id));
    }

    [Fact]
    public async Task PersistSnapshotAsync_DuplicateFingerprint_SoftDeletingTheFirstOwningRunDoesNotVanishTheTrade()
    {
        // The end-to-end regression for D16's membership fix: RecencyTrade
        // has ONE physical row per fingerprint, owned (RecencyRunId FK) by
        // whichever run inserted it first. Soft-deleting that first owner
        // must not remove the trade from recencyTrades as long as a second,
        // still-live run also produced the identical evidence.
        await using var database = await PostgresIntegrationTestDatabase.CreateMigratedAsync();
        await using var db = await OpenContextAsync(database.ConnectionString);
        var service = new RecencyPersistenceService(db, NullLogger<RecencyPersistenceService>.Instance);
        await SeedLaunchAsync(db, "launch-1");

        var sharedTrade = new RecencyTradeSnapshotRequest("fp-shared", 100, 200, 2m, 0.02m, 10m, 20m, 1);
        var first = await service.PersistSnapshotAsync(BuildRequest("launch-1", trades: new[] { sharedTrade }), CancellationToken.None);
        await service.PersistSnapshotAsync(BuildRequest("launch-1", trades: new[] { sharedTrade }), CancellationToken.None);

        // Soft-delete the FIRST run — the one that physically owns the row.
        var firstRun = await db.RecencyRuns.SingleAsync(r => r.Id == first.RecencyRunId);
        firstRun.DeletedAtMs = 999;
        await db.SaveChangesAsync();

        var query = new RecencyQuery();
        var trades = await query.GetRecencyTrades(db, fromMs: 0, toMs: 1000, symbols: null, strategies: null, CancellationToken.None);

        Assert.Single(trades);
        Assert.Equal("fp-shared", trades[0].Fingerprint);
    }

    [Fact]
    public async Task PersistSnapshotAsync_TombstonedLaunch_SkipsWithoutInsertingAnything()
    {
        await using var database = await PostgresIntegrationTestDatabase.CreateMigratedAsync();
        await using var db = await OpenContextAsync(database.ConnectionString);
        var service = new RecencyPersistenceService(db, NullLogger<RecencyPersistenceService>.Instance);
        await SeedLaunchAsync(db, "launch-1", deletedAtMs: 5_000);

        var result = await service.PersistSnapshotAsync(BuildRequest("launch-1"), CancellationToken.None);

        Assert.True(result.Skipped);
        Assert.Null(result.RecencyRunId);
        Assert.Equal(0, await db.RecencyRuns.CountAsync());
        Assert.Equal(0, await db.RecencyTrades.CountAsync());
    }

    [Fact]
    public async Task PersistSnapshotAsync_UnknownLaunch_ThrowsInvalidOperation()
    {
        await using var database = await PostgresIntegrationTestDatabase.CreateMigratedAsync();
        await using var db = await OpenContextAsync(database.ConnectionString);
        var service = new RecencyPersistenceService(db, NullLogger<RecencyPersistenceService>.Instance);

        await Assert.ThrowsAsync<InvalidOperationException>(
            () => service.PersistSnapshotAsync(BuildRequest("does-not-exist"), CancellationToken.None));
    }

    [Fact]
    public async Task PersistSnapshotAsync_ConcurrentSnapshotsForOneLaunch_SucceededRunsCountsEveryOne()
    {
        await using var database = await PostgresIntegrationTestDatabase.CreateMigratedAsync();

        await using (var seedDb = await OpenContextAsync(database.ConnectionString))
        {
            await SeedLaunchAsync(seedDb, "launch-1");
        }

        const int concurrentRuns = 8;
        var tasks = Enumerable.Range(0, concurrentRuns).Select(async i =>
        {
            await using var db = await OpenContextAsync(database.ConnectionString);
            var service = new RecencyPersistenceService(db, NullLogger<RecencyPersistenceService>.Instance);
            await service.PersistSnapshotAsync(BuildRequest("launch-1", symbol: $"SYM{i}", trades: new[]
            {
                new RecencyTradeSnapshotRequest($"fp-{i}", 100, 200, 2.0m, 0.02m, 10m, 20.0m, 1),
            }), CancellationToken.None);
        });
        await Task.WhenAll(tasks);

        await using var verifyDb = await OpenContextAsync(database.ConnectionString);
        var launch = await verifyDb.RecencyLaunches.SingleAsync(l => l.Id == "launch-1");
        Assert.Equal(concurrentRuns, launch.SucceededRuns);
    }

    [Fact]
    public async Task PersistSnapshotAsync_ConcurrentSnapshotsSharingAFingerprint_NeitherRunLosesItsOtherTrades()
    {
        await using var database = await PostgresIntegrationTestDatabase.CreateMigratedAsync();

        await using (var seedDb = await OpenContextAsync(database.ConnectionString))
        {
            await SeedLaunchAsync(seedDb, "launch-1");
        }

        // Both requests carry one shared fingerprint (the same canonical
        // evidence, e.g. an overlapping re-run window) plus a distinct
        // fingerprint of their own — a collision on the shared row must not
        // cost either run its distinct trade.
        var requestA = BuildRequest("launch-1", symbol: "SPY", trades: new[]
        {
            new RecencyTradeSnapshotRequest("fp-shared", 100, 200, 2.0m, 0.02m, 10m, 20.0m, 1),
            new RecencyTradeSnapshotRequest("fp-only-a", 300, 400, 2.0m, 0.02m, 10m, 20.0m, 1),
        });
        var requestB = BuildRequest("launch-1", symbol: "SPY", trades: new[]
        {
            new RecencyTradeSnapshotRequest("fp-shared", 100, 200, 2.0m, 0.02m, 10m, 20.0m, 1),
            new RecencyTradeSnapshotRequest("fp-only-b", 500, 600, 2.0m, 0.02m, 10m, 20.0m, 1),
        });

        await using var dbA = await OpenContextAsync(database.ConnectionString);
        await using var dbB = await OpenContextAsync(database.ConnectionString);
        var serviceA = new RecencyPersistenceService(dbA, NullLogger<RecencyPersistenceService>.Instance);
        var serviceB = new RecencyPersistenceService(dbB, NullLogger<RecencyPersistenceService>.Instance);

        var taskA = serviceA.PersistSnapshotAsync(requestA, CancellationToken.None);
        var taskB = serviceB.PersistSnapshotAsync(requestB, CancellationToken.None);
        var results = await Task.WhenAll(taskA, taskB);

        Assert.All(results, r => Assert.False(r.Skipped));

        await using var verifyDb = await OpenContextAsync(database.ConnectionString);
        Assert.Equal(1, await verifyDb.RecencyTrades.CountAsync(t => t.Fingerprint == "fp-shared"));
        Assert.Equal(1, await verifyDb.RecencyTrades.CountAsync(t => t.Fingerprint == "fp-only-a"));
        Assert.Equal(1, await verifyDb.RecencyTrades.CountAsync(t => t.Fingerprint == "fp-only-b"));

        var launch = await verifyDb.RecencyLaunches.SingleAsync(l => l.Id == "launch-1");
        Assert.Equal(2, launch.SucceededRuns);
    }

    [Fact]
    public async Task PersistSnapshotAsync_BlocksOnAConcurrentTransactionHoldingTheLaunchRowLock()
    {
        // Proves the tombstone check is actually atomic with the insert:
        // a persist call must block while another transaction holds a
        // FOR UPDATE lock on the same launch row, not read a stale
        // "not deleted" snapshot and race ahead of a concurrent
        // soft-delete. A plain pre-check-then-insert (the prior bug)
        // would let this call proceed immediately regardless of the held
        // lock.
        await using var database = await PostgresIntegrationTestDatabase.CreateMigratedAsync();
        await using (var seedDb = await OpenContextAsync(database.ConnectionString))
        {
            await SeedLaunchAsync(seedDb, "launch-1");
        }

        await using var lockHolderDb = await OpenContextAsync(database.ConnectionString);
        await using var lockHolderTx = await lockHolderDb.Database.BeginTransactionAsync();
        await lockHolderDb.Database
            .SqlQuery<long?>($"SELECT \"DeletedAtMs\" AS \"Value\" FROM \"RecencyLaunches\" WHERE \"Id\" = 'launch-1' FOR UPDATE")
            .SingleAsync();

        await using var db = await OpenContextAsync(database.ConnectionString);
        var service = new RecencyPersistenceService(db, NullLogger<RecencyPersistenceService>.Instance);
        var persistTask = service.PersistSnapshotAsync(BuildRequest("launch-1"), CancellationToken.None);

        var completedFirst = await Task.WhenAny(persistTask, Task.Delay(TimeSpan.FromSeconds(2)));
        Assert.NotSame(persistTask, completedFirst); // still blocked while the lock is held

        await lockHolderTx.CommitAsync();
        var result = await persistTask; // now unblocks

        Assert.False(result.Skipped);
    }
}

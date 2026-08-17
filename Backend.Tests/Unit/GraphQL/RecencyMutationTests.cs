using Backend.Data;
using Backend.GraphQL;
using Backend.Models.MarketData;
using Backend.Tests.Helpers;
using Microsoft.EntityFrameworkCore;

namespace Backend.Tests.Unit.GraphQL;

/// <summary>
/// Soft-delete / restore mutations (design spec D17). Tested at the
/// resolver-method level, mirroring RecencyQueryTests' precedent — this
/// repo has no IRequestExecutor test-schema harness.
/// </summary>
public class RecencyMutationTests
{
    private static async Task<AppDbContext> SeededDbAsync()
    {
        var db = TestDbContextFactory.Create();
        db.RecencyLaunches.Add(new RecencyLaunch { Id = "l1", ConfigJson = "{}", Status = "RUNNING", CreatedAtMs = 1 });
        await db.SaveChangesAsync();
        db.RecencyRuns.Add(new RecencyRun
        {
            Id = 1,
            RecencyLaunchId = "l1",
            Symbol = "SPY",
            StrategyKey = "ema_crossover_2_bps",
            ParamsJson = "{}",
            ParamsHash = "h1",
            TotalPnl = 10m,
            CreatedAtMs = 1,
        });
        await db.SaveChangesAsync();
        return db;
    }

    [Fact]
    public async Task SoftDeleteRecencyRunAsync_SetsTombstone()
    {
        var db = await SeededDbAsync();

        var result = await RecencyMutation.SoftDeleteRecencyRunAsync(db, 1, CancellationToken.None);

        Assert.IsType<RecencyRunMutationSuccess>(result);
        var run = await db.RecencyRuns.SingleAsync(r => r.Id == 1);
        Assert.NotNull(run.DeletedAtMs);
    }

    [Fact]
    public async Task SoftDeleteRecencyRunAsync_UnknownId_ReturnsTypedError()
    {
        var db = await SeededDbAsync();

        var result = await RecencyMutation.SoftDeleteRecencyRunAsync(db, 999, CancellationToken.None);

        var error = Assert.IsType<RecencyRunNotFoundError>(result);
        Assert.Equal("RECENCY_RUN_NOT_FOUND", error.Code);
    }

    [Fact]
    public async Task RestoreRecencyRunAsync_ClearsTombstone()
    {
        var db = await SeededDbAsync();
        var run = await db.RecencyRuns.SingleAsync(r => r.Id == 1);
        run.DeletedAtMs = 500;
        await db.SaveChangesAsync();

        var result = await RecencyMutation.RestoreRecencyRunAsync(db, 1, CancellationToken.None);

        Assert.IsType<RecencyRunMutationSuccess>(result);
        var restored = await db.RecencyRuns.SingleAsync(r => r.Id == 1);
        Assert.Null(restored.DeletedAtMs);
    }

    [Fact]
    public async Task SoftDeleteRecencyLaunchAsync_SetsTombstone()
    {
        var db = await SeededDbAsync();

        var result = await RecencyMutation.SoftDeleteRecencyLaunchAsync(db, "l1", CancellationToken.None);

        Assert.IsType<RecencyLaunchMutationSuccess>(result);
        var launch = await db.RecencyLaunches.SingleAsync(l => l.Id == "l1");
        Assert.NotNull(launch.DeletedAtMs);
    }

    [Fact]
    public async Task RestoreRecencyLaunchAsync_ClearsTombstone()
    {
        var db = await SeededDbAsync();
        var launch = await db.RecencyLaunches.SingleAsync(l => l.Id == "l1");
        launch.DeletedAtMs = 500;
        await db.SaveChangesAsync();

        var result = await RecencyMutation.RestoreRecencyLaunchAsync(db, "l1", CancellationToken.None);

        Assert.IsType<RecencyLaunchMutationSuccess>(result);
        var restored = await db.RecencyLaunches.SingleAsync(l => l.Id == "l1");
        Assert.Null(restored.DeletedAtMs);
    }

    [Fact]
    public async Task SoftDeleteRecencyLaunchAsync_ExcludesItsRunsFromTheProjectionWithoutCascading()
    {
        var db = await SeededDbAsync();
        var trade = new RecencyTrade
        {
            RecencyRunId = 1,
            Fingerprint = "fp1",
            EntryMs = 100,
            ExitMs = 200,
            PnlPts = 1m,
            PnlPct = 0.01m,
            Quantity = 10m,
            Pnl = 10m,
            HoldingSessions = 1,
        };
        db.RecencyTrades.Add(trade);
        await db.SaveChangesAsync();
        db.RecencyTradeMemberships.Add(new RecencyTradeMembership
        {
            RecencyTradeId = trade.Id,
            RecencyRunId = 1,
        });
        await db.SaveChangesAsync();

        await RecencyMutation.SoftDeleteRecencyLaunchAsync(db, "l1", CancellationToken.None);

        // The run itself is NOT individually tombstoned...
        var run = await db.RecencyRuns.SingleAsync(r => r.Id == 1);
        Assert.Null(run.DeletedAtMs);

        // ...but the projection still excludes it via the launch join.
        var trades = await RecencyQuery.GetRecencyTrades(db, 0, 1000, null, null, CancellationToken.None);
        Assert.Empty(trades);
    }
}

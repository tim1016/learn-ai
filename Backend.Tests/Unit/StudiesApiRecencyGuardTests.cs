using Backend.Models.MarketData;
using Backend.Tests.Helpers;

namespace Backend.Tests.Unit;

/// <summary>
/// Hard-delete guard (design spec D22, P0-4): a study backing a live
/// (non-tombstoned) RecencyRun must not be deletable via
/// DELETE /api/studies/{id} — deleting it out from under the Recency
/// Chart would break "forever until you soft-delete it" (D17).
/// </summary>
public class StudiesApiRecencyGuardTests
{
    [Fact]
    public async Task IsRecencyMemberAsync_StudyBackingALiveRecencyRun_ReturnsTrue()
    {
        var db = TestDbContextFactory.Create();
        db.RecencyLaunches.Add(new RecencyLaunch { Id = "l1", ConfigJson = "{}", Status = "RUNNING", CreatedAtMs = 1 });
        await db.SaveChangesAsync();
        db.RecencyRuns.Add(new RecencyRun
        {
            RecencyLaunchId = "l1",
            Symbol = "SPY",
            StrategyKey = "ema_crossover_2_bps",
            ParamsJson = "{}",
            ParamsHash = "h1",
            TotalPnl = 10m,
            CreatedAtMs = 1,
            StudyId = 42,
        });
        await db.SaveChangesAsync();

        var result = await StudiesApi.IsRecencyMemberAsync(db, 42, CancellationToken.None);

        Assert.True(result);
    }

    [Fact]
    public async Task IsRecencyMemberAsync_StudyNotReferencedByAnyRecencyRun_ReturnsFalse()
    {
        var db = TestDbContextFactory.Create();

        var result = await StudiesApi.IsRecencyMemberAsync(db, 99, CancellationToken.None);

        Assert.False(result);
    }

    [Fact]
    public async Task IsRecencyMemberAsync_OnlyReferencedBySoftDeletedRun_ReturnsFalse()
    {
        var db = TestDbContextFactory.Create();
        db.RecencyLaunches.Add(new RecencyLaunch { Id = "l1", ConfigJson = "{}", Status = "RUNNING", CreatedAtMs = 1 });
        await db.SaveChangesAsync();
        db.RecencyRuns.Add(new RecencyRun
        {
            RecencyLaunchId = "l1",
            Symbol = "SPY",
            StrategyKey = "ema_crossover_2_bps",
            ParamsJson = "{}",
            ParamsHash = "h1",
            TotalPnl = 10m,
            CreatedAtMs = 1,
            StudyId = 42,
            DeletedAtMs = 500,
        });
        await db.SaveChangesAsync();

        // A study whose ONLY recency membership was already soft-deleted is
        // no longer "live" — its hard-delete guard does not need to block.
        var result = await StudiesApi.IsRecencyMemberAsync(db, 42, CancellationToken.None);

        Assert.False(result);
    }
}

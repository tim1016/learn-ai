using Backend.Data;
using HotChocolate;
using HotChocolate.Types;
using Microsoft.EntityFrameworkCore;

namespace Backend.GraphQL;

/// <summary>
/// Soft-delete / restore mutations for the Recency Chart (design spec
/// D17). A launch-level soft-delete does not cascade a tombstone onto its
/// individual runs — the projection's join through RecencyLaunch already
/// excludes every run under a tombstoned launch, so a bulk "delete
/// launch" is a single write, not N.
/// </summary>
[ExtendObjectType<Mutation>]
public class RecencyMutation
{
    [GraphQLName("softDeleteRecencyRun")]
    public async Task<bool> SoftDeleteRecencyRunAsync(AppDbContext db, int runId, CancellationToken ct)
    {
        var run = await FindRunOrThrowAsync(db, runId, ct);
        run.DeletedAtMs = DateTimeOffset.UtcNow.ToUnixTimeMilliseconds();
        await db.SaveChangesAsync(ct);
        return true;
    }

    [GraphQLName("restoreRecencyRun")]
    public async Task<bool> RestoreRecencyRunAsync(AppDbContext db, int runId, CancellationToken ct)
    {
        var run = await FindRunOrThrowAsync(db, runId, ct);
        run.DeletedAtMs = null;
        await db.SaveChangesAsync(ct);
        return true;
    }

    [GraphQLName("softDeleteRecencyLaunch")]
    public async Task<bool> SoftDeleteRecencyLaunchAsync(AppDbContext db, string launchId, CancellationToken ct)
    {
        var launch = await FindLaunchOrThrowAsync(db, launchId, ct);
        launch.DeletedAtMs = DateTimeOffset.UtcNow.ToUnixTimeMilliseconds();
        await db.SaveChangesAsync(ct);
        return true;
    }

    [GraphQLName("restoreRecencyLaunch")]
    public async Task<bool> RestoreRecencyLaunchAsync(AppDbContext db, string launchId, CancellationToken ct)
    {
        var launch = await FindLaunchOrThrowAsync(db, launchId, ct);
        launch.DeletedAtMs = null;
        await db.SaveChangesAsync(ct);
        return true;
    }

    private static async Task<Models.MarketData.RecencyRun> FindRunOrThrowAsync(AppDbContext db, int runId, CancellationToken ct)
    {
        var run = await db.RecencyRuns.FirstOrDefaultAsync(r => r.Id == runId, ct);
        if (run is null)
        {
            throw new GraphQLException(
                ErrorBuilder.New()
                    .SetMessage($"RecencyRun {runId} not found")
                    .SetCode("RECENCY_RUN_NOT_FOUND")
                    .Build());
        }
        return run;
    }

    private static async Task<Models.MarketData.RecencyLaunch> FindLaunchOrThrowAsync(AppDbContext db, string launchId, CancellationToken ct)
    {
        var launch = await db.RecencyLaunches.FirstOrDefaultAsync(l => l.Id == launchId, ct);
        if (launch is null)
        {
            throw new GraphQLException(
                ErrorBuilder.New()
                    .SetMessage($"RecencyLaunch {launchId} not found")
                    .SetCode("RECENCY_LAUNCH_NOT_FOUND")
                    .Build());
        }
        return launch;
    }
}

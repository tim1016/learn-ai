using Backend.Data;
using HotChocolate;
using HotChocolate.Types;
using Microsoft.EntityFrameworkCore;

namespace Backend.GraphQL;

[UnionType]
public interface IRecencyRunMutationResult;
public sealed record RecencyRunMutationSuccess(int RecencyRunId) : IRecencyRunMutationResult;
public sealed record RecencyRunNotFoundError(string Message, string Code) : IRecencyRunMutationResult;

[UnionType]
public interface IRecencyLaunchMutationResult;
public sealed record RecencyLaunchMutationSuccess(string LaunchId) : IRecencyLaunchMutationResult;
public sealed record RecencyLaunchNotFoundError(string Message, string Code) : IRecencyLaunchMutationResult;

/// <summary>Soft-delete and restore mutations for Recency Chart evidence.</summary>
[ExtendObjectType(typeof(Mutation))]
public static class RecencyMutation
{
    [GraphQLName("softDeleteRecencyRun")]
    public static async Task<IRecencyRunMutationResult> SoftDeleteRecencyRunAsync(
        [Service] AppDbContext db,
        int runId,
        CancellationToken ct)
    {
        var run = await db.RecencyRuns.FirstOrDefaultAsync(r => r.Id == runId, ct);
        if (run is null)
            return new RecencyRunNotFoundError($"RecencyRun {runId} not found", "RECENCY_RUN_NOT_FOUND");

        run.DeletedAtMs = DateTimeOffset.UtcNow.ToUnixTimeMilliseconds();
        await db.SaveChangesAsync(ct);
        return new RecencyRunMutationSuccess(runId);
    }

    [GraphQLName("restoreRecencyRun")]
    public static async Task<IRecencyRunMutationResult> RestoreRecencyRunAsync(
        [Service] AppDbContext db,
        int runId,
        CancellationToken ct)
    {
        var run = await db.RecencyRuns.FirstOrDefaultAsync(r => r.Id == runId, ct);
        if (run is null)
            return new RecencyRunNotFoundError($"RecencyRun {runId} not found", "RECENCY_RUN_NOT_FOUND");

        run.DeletedAtMs = null;
        await db.SaveChangesAsync(ct);
        return new RecencyRunMutationSuccess(runId);
    }

    [GraphQLName("softDeleteRecencyLaunch")]
    public static async Task<IRecencyLaunchMutationResult> SoftDeleteRecencyLaunchAsync(
        [Service] AppDbContext db,
        string launchId,
        CancellationToken ct)
    {
        var launch = await db.RecencyLaunches.FirstOrDefaultAsync(l => l.Id == launchId, ct);
        if (launch is null)
            return new RecencyLaunchNotFoundError($"RecencyLaunch {launchId} not found", "RECENCY_LAUNCH_NOT_FOUND");

        launch.DeletedAtMs = DateTimeOffset.UtcNow.ToUnixTimeMilliseconds();
        await db.SaveChangesAsync(ct);
        return new RecencyLaunchMutationSuccess(launchId);
    }

    [GraphQLName("restoreRecencyLaunch")]
    public static async Task<IRecencyLaunchMutationResult> RestoreRecencyLaunchAsync(
        [Service] AppDbContext db,
        string launchId,
        CancellationToken ct)
    {
        var launch = await db.RecencyLaunches.FirstOrDefaultAsync(l => l.Id == launchId, ct);
        if (launch is null)
            return new RecencyLaunchNotFoundError($"RecencyLaunch {launchId} not found", "RECENCY_LAUNCH_NOT_FOUND");

        launch.DeletedAtMs = null;
        await db.SaveChangesAsync(ct);
        return new RecencyLaunchMutationSuccess(launchId);
    }
}

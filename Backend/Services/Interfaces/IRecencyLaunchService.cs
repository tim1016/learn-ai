namespace Backend.Services.Interfaces;

public interface IRecencyLaunchService
{
    /// <summary>
    /// Persist a RUNNING RecencyLaunch row before the job is dispatched to
    /// Python (design spec D20), so the launch — and its configuration —
    /// survives a zero-success run, a cancellation, or Redis's 24h job-state
    /// TTL. Idempotent: a retried call for the same id is a no-op.
    /// </summary>
    Task CreateLaunchAsync(string launchId, string configJson, int expectedRuns, CancellationToken ct);

    /// <summary>
    /// Set a durable terminal state. Counts are optional for cancellation,
    /// where the latest atomically persisted success count remains authoritative.
    /// Returns false when the launch does not exist.
    /// </summary>
    Task<bool> SetTerminalStatusAsync(
        string launchId,
        string status,
        int? succeededRuns,
        int? failedRuns,
        CancellationToken ct);
}

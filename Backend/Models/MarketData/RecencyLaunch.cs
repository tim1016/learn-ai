namespace Backend.Models.MarketData;

/// <summary>
/// One Recency Chart launch — persisted BEFORE the job is dispatched to
/// Python, so the launch (and its configuration) survives a zero-success
/// run, a cancellation, or Redis's 24h job-state TTL. The primary key is
/// the job id minted by <c>JobsApi.StartJobAsync</c>, so no separate id
/// mapping is needed between the job framework and this durable record.
/// </summary>
public class RecencyLaunch
{
    public string Id { get; set; } = "";

    public string ConfigJson { get; set; } = "";

    public int ExpectedRuns { get; set; }

    public int SucceededRuns { get; set; }

    public int FailedRuns { get; set; }

    /// RUNNING | COMPLETED | CANCELLED | FAILED
    public string Status { get; set; } = "RUNNING";

    public long CreatedAtMs { get; set; }

    public long? CompletedAtMs { get; set; }

    /// Soft-delete tombstone. A non-null value excludes the launch (and every
    /// run under it) from the recency projection and stops any in-flight
    /// child persist calls from writing further runs (design spec D17).
    public long? DeletedAtMs { get; set; }

    public ICollection<RecencyRun> Runs { get; set; } = new List<RecencyRun>();
}

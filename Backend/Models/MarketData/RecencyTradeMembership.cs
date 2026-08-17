namespace Backend.Models.MarketData;

/// <summary>
/// Records that <see cref="RecencyRun"/> also produced the canonical
/// evidence in <see cref="RecencyTrade"/> — even though the fingerprint's
/// database-level unique constraint means only ONE physical
/// <see cref="RecencyTrade"/> row exists per fingerprint, MULTIPLE runs
/// (typically re-launches whose generation windows overlap) can each
/// independently produce that identical evidence. Without this table the
/// duplicate claim was discarded entirely: soft-deleting whichever run
/// happened to insert the row first silently vanished the trade from
/// <c>recencyTrades</c> even though a second, still-live run also vouches
/// for it. A trade is live iff at least one membership's run and launch
/// are both non-deleted (see RecencyQuery.GetRecencyTrades).
/// </summary>
public class RecencyTradeMembership
{
    public int Id { get; set; }

    public int RecencyTradeId { get; set; }
    public RecencyTrade RecencyTrade { get; set; } = null!;

    public int RecencyRunId { get; set; }
    public RecencyRun RecencyRun { get; set; } = null!;
}

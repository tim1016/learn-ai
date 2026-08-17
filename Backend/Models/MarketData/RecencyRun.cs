namespace Backend.Models.MarketData;

/// <summary>
/// One (symbol, strategy, parameter-combo) execution within a Recency
/// Chart launch. <see cref="TotalPnl"/> and <see cref="Sharpe"/> are
/// Python-authored (AGENTS.md #5) and persisted verbatim — this row never
/// recomputes them.
/// </summary>
public class RecencyRun
{
    public int Id { get; set; }

    public string RecencyLaunchId { get; set; } = "";
    public RecencyLaunch RecencyLaunch { get; set; } = null!;

    public string StrategyKey { get; set; } = "";

    public string Symbol { get; set; } = "";

    public string ParamsJson { get; set; } = "";

    public string ParamsHash { get; set; } = "";

    /// Best-effort link to the underlying StrategyExecution study, for
    /// "open run" — populated only when the engine's own best-effort
    /// auto-save succeeded. Never the recency persistence's authority.
    public int? StudyId { get; set; }

    public decimal TotalPnl { get; set; }

    public decimal? Sharpe { get; set; }

    public long CreatedAtMs { get; set; }

    /// Soft-delete tombstone (design spec D17). Restorable.
    public long? DeletedAtMs { get; set; }

    public ICollection<RecencyTrade> Trades { get; set; } = new List<RecencyTrade>();
}

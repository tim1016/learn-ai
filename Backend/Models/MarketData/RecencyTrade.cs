namespace Backend.Models.MarketData;

/// <summary>
/// One trade in the Recency Chart's own trade snapshot — deliberately
/// separate from <see cref="BacktestTrade"/> because that table stores
/// dollar PnL only, and a Recency Chart display needs Python-authored
/// <see cref="PnlPct"/> and <see cref="HoldingSessions"/> as well
/// (AGENTS.md #5 — .NET never derives them). <see cref="Fingerprint"/>
/// carries a database-level unique constraint (see AppDbContext) so no
/// two rows can ever represent the same canonical evidence (design spec
/// D16) — the mechanism that makes a re-run of the same combo over an
/// overlapping window idempotent rather than duplicative.
///
/// Timestamps are int64 ms UTC per .claude/rules/temporal-rigor.md — this
/// intentionally diverges from BacktestTrade's DateTime columns, which
/// predate that rule.
/// </summary>
public class RecencyTrade
{
    public int Id { get; set; }

    public int RecencyRunId { get; set; }
    public RecencyRun RecencyRun { get; set; } = null!;

    public string Fingerprint { get; set; } = "";

    public long EntryMs { get; set; }

    public long ExitMs { get; set; }

    public decimal PnlPts { get; set; }

    public decimal PnlPct { get; set; }

    public decimal Quantity { get; set; }

    public decimal Pnl { get; set; }

    public int HoldingSessions { get; set; }

    /// <summary>
    /// True when the engine force-closed this position at the backtest
    /// window boundary rather than the strategy's own exit rule firing.
    /// Python-authored (EngineTradeResponse.is_synthetic_exit) — carried
    /// through verbatim so the chart can distinguish an end-of-window
    /// liquidation from an ordinary strategy exit.
    /// </summary>
    public bool IsSyntheticExit { get; set; }

    public string SignalReason { get; set; } = "";

    public ICollection<RecencyTradeMembership> Memberships { get; set; } = new List<RecencyTradeMembership>();
}

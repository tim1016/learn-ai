namespace Backend.GraphQL.Types;

/// <summary>
/// One trade in the recency projection. Every numeric field (PnlPts,
/// PnlPct, Sharpe, HoldingSessions) is Python-authored (AGENTS.md #5) and
/// passed through verbatim from RecencyTrade / its owning RecencyRun —
/// this type performs no computation.
/// </summary>
public class RecencyTradeType
{
    public string Symbol { get; set; } = "";
    public string StrategyKey { get; set; } = "";
    public string ParamsHash { get; set; } = "";
    public string ParamsJson { get; set; } = "";
    public string Fingerprint { get; set; } = "";
    public long EntryMs { get; set; }
    public long ExitMs { get; set; }
    public decimal PnlPts { get; set; }
    public decimal PnlPct { get; set; }
    public decimal Quantity { get; set; }
    public decimal Pnl { get; set; }
    public int HoldingSessions { get; set; }
    public decimal? Sharpe { get; set; }
    public int? StudyId { get; set; }
    public int RecencyRunId { get; set; }
    public bool IsSyntheticExit { get; set; }
    public string SignalReason { get; set; } = "";
}

/// <summary>
/// The hero (highest total-PnL) parameter combo for one (symbol, strategy)
/// pair. TotalPnl is Python-authored; selecting the maximum among rows is
/// a comparison, not a derivation (AGENTS.md #5 draws that line at .NET
/// computing a number a user will compare against another — MAX/GROUP BY
/// selects among already-authored numbers, it does not compute one).
/// </summary>
public class RecencyHeroType
{
    public string Symbol { get; set; } = "";
    public string StrategyKey { get; set; } = "";
    public string ParamsHash { get; set; } = "";
    public decimal TotalPnl { get; set; }
    public int RecencyRunId { get; set; }
}

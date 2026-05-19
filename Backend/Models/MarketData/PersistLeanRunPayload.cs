namespace Backend.Models.MarketData;

public record PersistLeanRunPayload(
    string LeanRunId,
    string Source,
    string StrategyName,
    string Symbol,
    decimal StartingCash,
    long StartDateMs,
    long EndDateMs,
    int TotalTrades,
    int WinningTrades,
    int LosingTrades,
    decimal TotalPnl,
    decimal TotalFees,
    decimal FinalEquity,
    double WinRate,
    IReadOnlyList<PersistLeanTradePayload> Trades,
    Dictionary<string, object>? LeanStatistics);

public record PersistLeanTradePayload(
    int TradeNumber,
    long EntryMsUtc,
    long ExitMsUtc,
    decimal EntryPrice,
    decimal ExitPrice,
    decimal Quantity,
    decimal Pnl,
    string SignalReason,
    bool IsSyntheticExit);

using System.Text.Json.Serialization;

namespace Backend.Models.MarketData;

/// <summary>
/// Payload posted by PythonDataService after a backtest run completes.
/// Used by both the LEAN sidecar (source="lean-sidecar", lean_run_id required)
/// and the in-process engine (source="engine", lean_run_id=null).
/// JsonPropertyName attributes accept the snake_case wire format Python sends
/// while keeping PascalCase property names for C# consumers.
/// </summary>
public record PersistLeanRunPayload(
    [property: JsonPropertyName("lean_run_id")] string? LeanRunId,
    [property: JsonPropertyName("source")] string Source,
    [property: JsonPropertyName("strategy_name")] string StrategyName,
    [property: JsonPropertyName("symbol")] string Symbol,
    [property: JsonPropertyName("starting_cash")] decimal StartingCash,
    [property: JsonPropertyName("start_date_ms")] long StartDateMs,
    [property: JsonPropertyName("end_date_ms")] long EndDateMs,
    [property: JsonPropertyName("total_trades")] int TotalTrades,
    [property: JsonPropertyName("winning_trades")] int WinningTrades,
    [property: JsonPropertyName("losing_trades")] int LosingTrades,
    [property: JsonPropertyName("total_pnl")] decimal TotalPnl,
    [property: JsonPropertyName("total_fees")] decimal TotalFees,
    [property: JsonPropertyName("final_equity")] decimal FinalEquity,
    [property: JsonPropertyName("win_rate")] double WinRate,
    [property: JsonPropertyName("trades")] IReadOnlyList<PersistLeanTradePayload> Trades,
    [property: JsonPropertyName("lean_statistics")] Dictionary<string, object>? LeanStatistics);

public record PersistLeanTradePayload(
    [property: JsonPropertyName("trade_number")] int TradeNumber,
    [property: JsonPropertyName("entry_ms_utc")] long EntryMsUtc,
    [property: JsonPropertyName("exit_ms_utc")] long ExitMsUtc,
    [property: JsonPropertyName("entry_price")] decimal EntryPrice,
    [property: JsonPropertyName("exit_price")] decimal ExitPrice,
    [property: JsonPropertyName("quantity")] decimal Quantity,
    [property: JsonPropertyName("pnl")] decimal Pnl,
    [property: JsonPropertyName("signal_reason")] string SignalReason,
    [property: JsonPropertyName("is_synthetic_exit")] bool IsSyntheticExit);

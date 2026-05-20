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
    [property: JsonPropertyName("lean_statistics")] Dictionary<string, object>? LeanStatistics)
{
    /// <summary>
    /// PR B — canonical DataPolicy block as a JSON string (jsonb-bound on
    /// the .NET side). Optional: legacy callers omit it and the persistence
    /// service synthesizes a default block (``adjusted=true``, m/1→m/15,
    /// regular session) from ``Symbol`` to preserve the one-deprecation-cycle
    /// compat promise. New callers always send it.
    /// </summary>
    [JsonPropertyName("data_policy_json")]
    public string? DataPolicyJson { get; init; }

    /// <summary>
    /// PR B — commission per order in dollars. Python engine sends the
    /// configured commission; LEAN sends the fee actually charged by the
    /// fill model. Null from legacy callers; the persistence service
    /// defaults to 0 in that case.
    /// </summary>
    [JsonPropertyName("commission_per_order")]
    public decimal? CommissionPerOrder { get; init; }

    /// <summary>
    /// PR B — brokerage policy. Python engine writes
    /// ``"algorithm_default"`` because it doesn't model brokerage; LEAN
    /// writes whatever the manifest recorded.
    /// </summary>
    [JsonPropertyName("brokerage_policy")]
    public string? BrokeragePolicy { get; init; }
}

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

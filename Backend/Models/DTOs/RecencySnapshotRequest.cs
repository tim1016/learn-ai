using System.Text.Json.Serialization;

namespace Backend.Models.DTOs;

/// <summary>
/// Body of POST /api/recency/snapshots. JsonPropertyName attributes accept
/// the snake_case wire format Python's <c>persist_client.py</c> sends
/// (mirrors PersistLeanRunPayload's convention).
/// </summary>
public record RecencySnapshotRequest(
    [property: JsonPropertyName("launch_id")] string LaunchId,
    [property: JsonPropertyName("symbol")] string Symbol,
    [property: JsonPropertyName("strategy_key")] string StrategyKey,
    [property: JsonPropertyName("params")] Dictionary<string, double> Params,
    [property: JsonPropertyName("params_hash")] string ParamsHash,
    [property: JsonPropertyName("total_pnl")] decimal TotalPnl,
    [property: JsonPropertyName("sharpe")] decimal? Sharpe,
    [property: JsonPropertyName("trades")] IReadOnlyList<RecencyTradeSnapshotRequest> Trades);

public record RecencyTradeSnapshotRequest(
    [property: JsonPropertyName("fingerprint")] string Fingerprint,
    [property: JsonPropertyName("entry_ms")] long EntryMs,
    [property: JsonPropertyName("exit_ms")] long ExitMs,
    [property: JsonPropertyName("pnl_pts")] decimal PnlPts,
    [property: JsonPropertyName("pnl_pct")] decimal PnlPct,
    [property: JsonPropertyName("quantity")] decimal Quantity,
    [property: JsonPropertyName("pnl")] decimal Pnl,
    [property: JsonPropertyName("holding_sessions")] int HoldingSessions);

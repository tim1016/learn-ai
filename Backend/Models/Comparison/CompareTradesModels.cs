using System.Text.Json.Serialization;
using Backend.Models.MarketData;

namespace Backend.Models.Comparison;

/// <summary>
/// Request sent to Python's POST /api/lean-sidecar/compare.
///
/// [JsonPropertyName] on every property is the belt-and-suspenders fix
/// from PR #291 commit a818b4b8 — snake_case fields silently bound as
/// null when relying on PropertyNamingPolicy alone.
/// </summary>
public record CompareTradesRequest(
    [property: JsonPropertyName("left_trades")] IReadOnlyList<PersistLeanTradePayload> LeftTrades,
    [property: JsonPropertyName("right_trades")] IReadOnlyList<PersistLeanTradePayload> RightTrades,
    [property: JsonPropertyName("fill_price_atol")] decimal FillPriceAtol = 0.01m,
    [property: JsonPropertyName("assert_fees")] bool AssertFees = false);

/// <summary>
/// Response from Python's POST /api/lean-sidecar/compare.
/// </summary>
public record CompareTradesResponse(
    [property: JsonPropertyName("divergences")] IReadOnlyList<TradeDivergenceRecord> Divergences,
    [property: JsonPropertyName("first_divergence_ms_utc")] long? FirstDivergenceMsUtc);

/// <summary>
/// One classified divergence returned by the compare endpoint.
/// Optional numeric fields are null when the category doesn't apply
/// (e.g. DECISION_MISMATCH has no fill price to compare).
/// </summary>
public record TradeDivergenceRecord(
    [property: JsonPropertyName("category")] string Category,
    [property: JsonPropertyName("trade_number")] int? TradeNumber,
    [property: JsonPropertyName("ms_utc")] long? MsUtc,
    [property: JsonPropertyName("message")] string Message,
    [property: JsonPropertyName("left_fill_price")] decimal? LeftFillPrice = null,
    [property: JsonPropertyName("right_fill_price")] decimal? RightFillPrice = null,
    [property: JsonPropertyName("left_quantity")] decimal? LeftQuantity = null,
    [property: JsonPropertyName("right_quantity")] decimal? RightQuantity = null);

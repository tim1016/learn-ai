namespace Backend.Models.DTOs.PolygonResponses;

/// <summary>
/// DTO for individual aggregate (OHLCV) data from Python service.
/// Timestamp is Unix milliseconds (int64) — the canonical wire format.
/// </summary>
public class AggregateData
{
    public long Timestamp { get; set; }
    public decimal Open { get; set; }
    public decimal High { get; set; }
    public decimal Low { get; set; }
    public decimal Close { get; set; }
    public decimal Volume { get; set; }
    public decimal? Vwap { get; set; }
    public decimal? Transactions { get; set; }
}

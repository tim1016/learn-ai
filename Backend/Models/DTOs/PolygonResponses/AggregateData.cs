namespace Backend.Models.DTOs.PolygonResponses;

/// <summary>
/// DTO for individual aggregate (OHLCV) data from Python service
/// Matches Python SanitizedDataResponse.data schema
/// </summary>
public class AggregateData
{
    public required string Timestamp { get; set; }
    public decimal Open { get; set; }
    public decimal High { get; set; }
    public decimal Low { get; set; }
    public decimal Close { get; set; }
    public decimal Volume { get; set; }
    public decimal? Vwap { get; set; }
    public long? Transactions { get; set; }
}

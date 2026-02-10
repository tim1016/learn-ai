namespace Backend.Models.DTOs.PolygonResponses;

/// <summary>
/// Response from Python service /api/trades/fetch endpoint
/// </summary>
public class TradeResponse
{
    public bool Success { get; set; }
    public List<TradeData> Data { get; set; } = [];
    public DataSummary? Summary { get; set; }
    public required string Ticker { get; set; }
    public required string DataType { get; set; }
    public string? Error { get; set; }
}

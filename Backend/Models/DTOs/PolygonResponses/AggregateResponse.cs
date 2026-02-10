namespace Backend.Models.DTOs.PolygonResponses;

/// <summary>
/// Response from Python service /api/aggregates/fetch endpoint
/// Matches Python SanitizedDataResponse schema exactly
/// </summary>
public class AggregateResponse
{
    public bool Success { get; set; }
    public List<AggregateData> Data { get; set; } = [];
    public DataSummary? Summary { get; set; }
    public required string Ticker { get; set; }
    public required string DataType { get; set; }
    public string? Error { get; set; }
}

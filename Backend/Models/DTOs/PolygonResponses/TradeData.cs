namespace Backend.Models.DTOs.PolygonResponses;

/// <summary>
/// DTO for individual trade data from Python service
/// </summary>
public class TradeData
{
    public required string Timestamp { get; set; }
    public decimal Price { get; set; }
    public decimal Size { get; set; }
    public long? Exchange { get; set; }
    public string? Conditions { get; set; }
    public long? SequenceNumber { get; set; }
    public string? TradeId { get; set; }
}

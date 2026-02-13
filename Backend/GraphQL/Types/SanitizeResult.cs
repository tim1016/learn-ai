using Backend.Models.DTOs;

namespace Backend.GraphQL.Types;

public class SanitizeMarketDataResult
{
    public bool Success { get; set; }
    public List<MarketDataRecord> Data { get; set; } = [];
    public int OriginalCount { get; set; }
    public int CleanedCount { get; set; }
    public string? Message { get; set; }
}

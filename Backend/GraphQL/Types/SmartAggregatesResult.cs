namespace Backend.GraphQL.Types;

public class SmartAggregatesResult
{
    public required string Ticker { get; set; }
    public List<AggregateBar> Aggregates { get; set; } = [];
    public string? SanitizationSummary { get; set; }
}

/// <summary>
/// DTO for aggregate data — avoids exposing EF entity directly in GraphQL
/// </summary>
public class AggregateBar
{
    public long Id { get; set; }
    public decimal Open { get; set; }
    public decimal High { get; set; }
    public decimal Low { get; set; }
    public decimal Close { get; set; }
    public decimal Volume { get; set; }
    public decimal? VolumeWeightedAveragePrice { get; set; }
    public long Timestamp { get; set; }
    public string Timespan { get; set; } = "";
    public int Multiplier { get; set; }
    public long? TransactionCount { get; set; }
}

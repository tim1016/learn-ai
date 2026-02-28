namespace Backend.GraphQL.Types;

public class SmartAggregatesResult
{
    public required string Ticker { get; set; }
    public List<AggregateBar> Aggregates { get; set; } = [];
    public AggregatesSummary? Summary { get; set; }
    public string? SanitizationSummary { get; set; }
    public GapDetectionInfo? GapDetection { get; set; }
}

public class GapDetectionInfo
{
    public int TotalWeekdays { get; set; }
    public int DaysWithData { get; set; }
    public int MissingDays { get; set; }
    public int PartialDays { get; set; }
    public decimal CoveragePercent { get; set; }
    public int ExpectedBars { get; set; }
    public int ActualBars { get; set; }
    public List<string> MissingDates { get; set; } = [];
    public List<string> PartialDates { get; set; } = [];
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
    public DateTime Timestamp { get; set; }
    public string Timespan { get; set; } = "";
    public int Multiplier { get; set; }
    public long? TransactionCount { get; set; }
}

public class AggregatesSummary
{
    public decimal PeriodHigh { get; set; }
    public decimal PeriodLow { get; set; }
    public decimal AverageVolume { get; set; }
    public decimal? AverageVwap { get; set; }
    public decimal OpenPrice { get; set; }
    public decimal ClosePrice { get; set; }
    public decimal PriceChange { get; set; }
    public decimal PriceChangePercent { get; set; }
    public int TotalBars { get; set; }
}

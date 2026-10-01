namespace Backend.Models.DTOs.PolygonResponses;

public class SnapshotBarDto
{
    public decimal? Open { get; set; }
    public decimal? High { get; set; }
    public decimal? Low { get; set; }
    public decimal? Close { get; set; }
    public decimal? Volume { get; set; }
    public decimal? Vwap { get; set; }
}

public class MinuteBarDto : SnapshotBarDto
{
    public decimal? AccumulatedVolume { get; set; }
    public long? Timestamp { get; set; }
}

public class StockTickerSnapshotDto
{
    public string? Ticker { get; set; }
    public SnapshotBarDto? Day { get; set; }
    public SnapshotBarDto? PrevDay { get; set; }
    public MinuteBarDto? Min { get; set; }
    public decimal? TodaysChange { get; set; }
    public decimal? TodaysChangePercent { get; set; }
    public long? Updated { get; set; }
}

public class StockSnapshotResponse
{
    public bool Success { get; set; }
    public StockTickerSnapshotDto? Snapshot { get; set; }
    public string? Error { get; set; }
}

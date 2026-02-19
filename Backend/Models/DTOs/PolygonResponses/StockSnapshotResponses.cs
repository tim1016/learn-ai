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

public class StockSnapshotsResponse
{
    public bool Success { get; set; }
    public List<StockTickerSnapshotDto> Snapshots { get; set; } = [];
    public int Count { get; set; }
    public string? Error { get; set; }
}

public class MarketMoversResponse
{
    public bool Success { get; set; }
    public List<StockTickerSnapshotDto> Tickers { get; set; } = [];
    public int Count { get; set; }
    public string? Error { get; set; }
}

public class UnifiedSnapshotSessionDto
{
    public decimal? Price { get; set; }
    public decimal? Change { get; set; }
    public decimal? ChangePercent { get; set; }
    public decimal? Open { get; set; }
    public decimal? Close { get; set; }
    public decimal? High { get; set; }
    public decimal? Low { get; set; }
    public decimal? PreviousClose { get; set; }
    public decimal? Volume { get; set; }
}

public class UnifiedSnapshotItemDto
{
    public string? Ticker { get; set; }
    public string? Type { get; set; }
    public string? MarketStatus { get; set; }
    public string? Name { get; set; }
    public UnifiedSnapshotSessionDto? Session { get; set; }
}

public class UnifiedSnapshotResponse
{
    public bool Success { get; set; }
    public List<UnifiedSnapshotItemDto> Results { get; set; } = [];
    public int Count { get; set; }
    public string? Error { get; set; }
}

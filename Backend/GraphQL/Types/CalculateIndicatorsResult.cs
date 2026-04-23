namespace Backend.GraphQL.Types;

public class CalculateIndicatorsResult
{
    public bool Success { get; set; }
    public required string Ticker { get; set; }
    public List<IndicatorSeriesResult> Indicators { get; set; } = [];

    /// <summary>
    /// Error description when Success = false. Null on success.
    /// Renamed from Message (audit § 3.3) so Python's `error` field surfaces to the UI.
    /// </summary>
    public string? Error { get; set; }
}

public class IndicatorSeriesResult
{
    public required string Name { get; set; }
    public int Window { get; set; }
    public List<IndicatorPoint> Data { get; set; } = [];
}

public class IndicatorPoint
{
    public long Timestamp { get; set; }
    public decimal? Value { get; set; }
    public decimal? Signal { get; set; }
    public decimal? Histogram { get; set; }
    public decimal? Upper { get; set; }
    public decimal? Lower { get; set; }
}

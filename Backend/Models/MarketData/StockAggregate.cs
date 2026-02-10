namespace Backend.Models.MarketData;

/// <summary>
/// OHLCV (Open, High, Low, Close, Volume) aggregate bar
/// Represents price action for a specific time period
/// </summary>
public class StockAggregate
{
    public long Id { get; set; }

    public int TickerId { get; set; }
    public Ticker? Ticker { get; set; }

    // OHLCV data
    public decimal Open { get; set; }
    public decimal High { get; set; }
    public decimal Low { get; set; }
    public decimal Close { get; set; }
    public decimal Volume { get; set; }
    public decimal? VolumeWeightedAveragePrice { get; set; }

    // Timeframe
    public DateTime Timestamp { get; set; }
    public required string Timespan { get; set; }
    public int Multiplier { get; set; } = 1;

    // Metadata
    public long? TransactionCount { get; set; }
    public DateTime CreatedAt { get; set; } = DateTime.UtcNow;

    /// <summary>
    /// Validates OHLCV data integrity
    /// Testable business rule
    /// </summary>
    public bool IsValid()
    {
        return High >= Open &&
               High >= Close &&
               High >= Low &&
               Low <= Open &&
               Low <= Close &&
               Low <= High &&
               Volume >= 0;
    }
}

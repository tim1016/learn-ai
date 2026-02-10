namespace Backend.Models.MarketData;

/// <summary>
/// Technical indicator values (SMA, EMA, RSI, MACD)
/// </summary>
public class TechnicalIndicator
{
    public long Id { get; set; }

    public int TickerId { get; set; }
    public Ticker? Ticker { get; set; }

    public required string IndicatorType { get; set; }
    public DateTime Timestamp { get; set; }
    public required string Timespan { get; set; }
    public int Window { get; set; }

    // Values (nullable as different indicators have different fields)
    public decimal? Value { get; set; }
    public decimal? Signal { get; set; }
    public decimal? Histogram { get; set; }

    // Store full indicator data as JSON for flexibility
    public string? ValuesJson { get; set; }

    public DateTime CreatedAt { get; set; } = DateTime.UtcNow;
}

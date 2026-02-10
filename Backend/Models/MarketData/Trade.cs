namespace Backend.Models.MarketData;

/// <summary>
/// Individual trade record
/// </summary>
public class Trade
{
    public long Id { get; set; }

    public int TickerId { get; set; }
    public Ticker? Ticker { get; set; }

    public decimal Price { get; set; }
    public decimal Size { get; set; }
    public DateTime Timestamp { get; set; }

    public long? Exchange { get; set; }
    public string? Conditions { get; set; }
    public long? SequenceNumber { get; set; }
    public string? TradeId { get; set; }

    public DateTime CreatedAt { get; set; } = DateTime.UtcNow;
}

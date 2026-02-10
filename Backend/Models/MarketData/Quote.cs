namespace Backend.Models.MarketData;

/// <summary>
/// Bid/Ask quote data
/// </summary>
public class Quote
{
    public long Id { get; set; }

    public int TickerId { get; set; }
    public Ticker? Ticker { get; set; }

    public decimal BidPrice { get; set; }
    public decimal AskPrice { get; set; }
    public decimal BidSize { get; set; }
    public decimal AskSize { get; set; }

    public DateTime Timestamp { get; set; }

    public long? BidExchange { get; set; }
    public long? AskExchange { get; set; }
    public long? SequenceNumber { get; set; }

    public DateTime CreatedAt { get; set; } = DateTime.UtcNow;

    /// <summary>
    /// Calculate bid-ask spread (testable)
    /// </summary>
    public decimal GetSpread() => AskPrice - BidPrice;

    /// <summary>
    /// Calculate mid-point price (testable)
    /// </summary>
    public decimal GetMidPrice() => (BidPrice + AskPrice) / 2;
}

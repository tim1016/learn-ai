namespace Backend.Models.MarketData;

/// <summary>
/// Represents a financial instrument (stock, crypto, forex)
/// Pure entity model - no business logic, easily testable
/// </summary>
public class Ticker
{
    public int Id { get; set; }

    public required string Symbol { get; set; }

    public required string Name { get; set; }

    public required string Market { get; set; }

    public string? Locale { get; set; }

    public string? PrimaryExchange { get; set; }

    public string? Type { get; set; }

    public bool Active { get; set; } = true;

    public string? CurrencySymbol { get; set; }

    public DateTime CreatedAt { get; set; } = DateTime.UtcNow;

    public DateTime? UpdatedAt { get; set; }

    // Navigation properties
    public List<StockAggregate> Aggregates { get; set; } = [];
    public List<Trade> Trades { get; set; } = [];
    public List<Quote> Quotes { get; set; } = [];
    public List<TechnicalIndicator> Indicators { get; set; } = [];
}

namespace Backend.Models.MarketData;

/// <summary>
/// Reference data: splits, dividends, corporate actions
/// </summary>
public class ReferenceData
{
    public int Id { get; set; }

    public int TickerId { get; set; }
    public Ticker? Ticker { get; set; }

    public required string DataType { get; set; }
    public DateTime EventDate { get; set; }
    public DateTime? ExecutionDate { get; set; }

    public decimal? CashAmount { get; set; }
    public string? DeclarationDate { get; set; }
    public decimal? SplitFrom { get; set; }
    public decimal? SplitTo { get; set; }

    public string? MetadataJson { get; set; }

    public DateTime CreatedAt { get; set; } = DateTime.UtcNow;
}

namespace Backend.Models.Portfolio;

public class PortfolioSnapshot
{
    public Guid Id { get; set; }

    public Guid AccountId { get; set; }
    public Account Account { get; set; } = null!;

    public DateTime Timestamp { get; set; }

    public decimal Equity { get; set; }
    public decimal Cash { get; set; }
    public decimal MarketValue { get; set; }
    public decimal MarginUsed { get; set; }

    public decimal UnrealizedPnL { get; set; }
    public decimal RealizedPnL { get; set; }

    public decimal? NetDelta { get; set; }
    public decimal? NetGamma { get; set; }
    public decimal? NetTheta { get; set; }
    public decimal? NetVega { get; set; }
}

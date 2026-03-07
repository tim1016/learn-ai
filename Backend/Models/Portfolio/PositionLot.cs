namespace Backend.Models.Portfolio;

public class PositionLot
{
    public Guid Id { get; set; }

    public Guid PositionId { get; set; }
    public Position Position { get; set; } = null!;

    public Guid TradeId { get; set; }
    public PortfolioTrade Trade { get; set; } = null!;

    public decimal Quantity { get; set; }
    public decimal EntryPrice { get; set; }
    public decimal RemainingQuantity { get; set; }
    public decimal RealizedPnL { get; set; }

    public DateTime OpenedAt { get; set; }
    public DateTime? ClosedAt { get; set; }
}

using Backend.Models.MarketData;

namespace Backend.Models.Portfolio;

public class Position
{
    public Guid Id { get; set; }

    public Guid AccountId { get; set; }
    public Account Account { get; set; } = null!;

    public int TickerId { get; set; }
    public Ticker Ticker { get; set; } = null!;

    public AssetType AssetType { get; set; } = AssetType.Stock;

    public Guid? OptionContractId { get; set; }
    public OptionContract? OptionContract { get; set; }

    public decimal NetQuantity { get; set; }
    public decimal AvgCostBasis { get; set; }
    public decimal RealizedPnL { get; set; }

    public PositionStatus Status { get; set; } = PositionStatus.Open;

    public DateTime OpenedAt { get; set; }
    public DateTime? ClosedAt { get; set; }
    public DateTime LastUpdated { get; set; }

    // Navigation properties
    public List<PositionLot> Lots { get; set; } = [];
}

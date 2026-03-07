using Backend.Models.MarketData;

namespace Backend.Models.Portfolio;

public class PortfolioTrade
{
    public Guid Id { get; set; }

    public Guid AccountId { get; set; }
    public Account Account { get; set; } = null!;

    public Guid OrderId { get; set; }
    public Order Order { get; set; } = null!;

    public int TickerId { get; set; }
    public Ticker Ticker { get; set; } = null!;

    public OrderSide Side { get; set; }

    public decimal Quantity { get; set; }
    public decimal Price { get; set; }
    public decimal Fees { get; set; }

    public AssetType AssetType { get; set; } = AssetType.Stock;

    public Guid? OptionContractId { get; set; }
    public OptionContract? OptionContract { get; set; }

    public int Multiplier { get; set; } = 1;

    public DateTime ExecutionTimestamp { get; set; }

    // Navigation properties
    public List<PositionLot> Lots { get; set; } = [];
    public OptionLeg? OptionLeg { get; set; }
}

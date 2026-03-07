using System.ComponentModel.DataAnnotations;
using Backend.Models.MarketData;

namespace Backend.Models.Portfolio;

public class Order
{
    public Guid Id { get; set; }

    public Guid AccountId { get; set; }
    public Account Account { get; set; } = null!;

    public int TickerId { get; set; }
    public Ticker Ticker { get; set; } = null!;

    public OrderSide Side { get; set; }
    public OrderType OrderType { get; set; }

    public decimal Quantity { get; set; }
    public decimal? LimitPrice { get; set; }

    public OrderStatus Status { get; set; } = OrderStatus.Pending;

    public AssetType AssetType { get; set; } = AssetType.Stock;

    public Guid? OptionContractId { get; set; }
    public OptionContract? OptionContract { get; set; }

    public DateTime SubmittedAt { get; set; } = DateTime.UtcNow;
    public DateTime? FilledAt { get; set; }

    // Navigation properties
    public List<PortfolioTrade> Trades { get; set; } = [];
}

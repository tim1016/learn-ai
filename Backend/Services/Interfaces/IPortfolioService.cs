using Backend.Models.Portfolio;

namespace Backend.Services.Interfaces;

public interface IPortfolioService
{
    Task<Account> CreateAccountAsync(string name, AccountType type, decimal initialCash, CancellationToken ct = default);
    Task<Order> SubmitOrderAsync(Guid accountId, int tickerId, OrderSide side, OrderType orderType,
        decimal quantity, decimal? limitPrice, AssetType assetType = AssetType.Stock,
        Guid? optionContractId = null, CancellationToken ct = default);
    Task<Order> CancelOrderAsync(Guid orderId, CancellationToken ct = default);
    Task<PortfolioTrade> FillOrderAsync(Guid orderId, decimal fillPrice, decimal fillQuantity,
        decimal fees = 0, int multiplier = 1, OptionLegInput? optionLeg = null, CancellationToken ct = default);
    Task<PortfolioTrade> RecordTradeAsync(RecordTradeInput input, CancellationToken ct = default);
    Task<PortfolioState> GetPortfolioStateAsync(Guid accountId, CancellationToken ct = default);
}

public class OptionLegInput
{
    public Guid OptionContractId { get; set; }
    public decimal Quantity { get; set; }
    public decimal? EntryIV { get; set; }
    public decimal? EntryDelta { get; set; }
    public decimal? EntryGamma { get; set; }
    public decimal? EntryTheta { get; set; }
    public decimal? EntryVega { get; set; }
}

public class RecordTradeInput
{
    public Guid AccountId { get; set; }
    public string Symbol { get; set; } = string.Empty;
    public OrderSide Side { get; set; }
    public decimal Quantity { get; set; }
    public decimal Price { get; set; }
    public decimal Fees { get; set; }
    public AssetType AssetType { get; set; } = AssetType.Stock;
    public Guid? OptionContractId { get; set; }
    public int Multiplier { get; set; } = 1;
    public DateTime? ExecutionTimestamp { get; set; }
    public OptionLegInput? OptionLeg { get; set; }
}

public class PortfolioState
{
    public Account Account { get; set; } = null!;
    public List<Position> Positions { get; set; } = [];
    public List<PortfolioTrade> RecentTrades { get; set; } = [];
    public decimal TotalRealizedPnL { get; set; }
}

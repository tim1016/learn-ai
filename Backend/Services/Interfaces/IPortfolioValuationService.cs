using Backend.Models.Portfolio;

namespace Backend.Services.Interfaces;

public interface IPortfolioValuationService
{
    Task<PortfolioValuation> ComputeValuationAsync(Guid accountId, CancellationToken ct = default);
    Task<PortfolioValuation> ComputeValuationWithPricesAsync(Guid accountId,
        Dictionary<string, decimal> prices, CancellationToken ct = default);
}

public class PortfolioValuation
{
    public decimal Cash { get; set; }
    public decimal MarketValue { get; set; }
    public decimal Equity { get; set; }
    public decimal UnrealizedPnL { get; set; }
    public decimal RealizedPnL { get; set; }
    public decimal? NetDelta { get; set; }
    public decimal? NetGamma { get; set; }
    public decimal? NetTheta { get; set; }
    public decimal? NetVega { get; set; }
    public List<PositionValuation> Positions { get; set; } = [];
}

public class PositionValuation
{
    public Guid PositionId { get; set; }
    public string Symbol { get; set; } = "";
    public decimal Quantity { get; set; }
    public decimal CostBasis { get; set; }
    public decimal CurrentPrice { get; set; }
    public decimal MarketValue { get; set; }
    public decimal UnrealizedPnL { get; set; }
    public int Multiplier { get; set; } = 1;
}

using Backend.Models.Portfolio;

namespace Backend.Services.Interfaces;

public interface IStrategyAttributionService
{
    Task<StrategyTradeLink> LinkTradeToStrategyAsync(Guid tradeId, int strategyExecutionId,
        CancellationToken ct = default);
    Task<List<PortfolioTrade>> ImportBacktestTradesAsync(int strategyExecutionId, Guid accountId,
        CancellationToken ct = default);
    Task<StrategyPnLResult> GetStrategyPnLAsync(int strategyExecutionId, CancellationToken ct = default);
    Task<List<AlphaAttribution>> GetAlphaAttributionAsync(Guid accountId, CancellationToken ct = default);
}

public class StrategyPnLResult
{
    public int StrategyExecutionId { get; set; }
    public string StrategyName { get; set; } = "";
    public decimal TotalPnL { get; set; }
    public int TradeCount { get; set; }
    public decimal WinRate { get; set; }
}

public class AlphaAttribution
{
    public int StrategyExecutionId { get; set; }
    public string StrategyName { get; set; } = "";
    public decimal PnL { get; set; }
    public decimal ContributionPercent { get; set; }
    public int TradeCount { get; set; }
}

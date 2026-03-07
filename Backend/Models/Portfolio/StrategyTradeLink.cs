using Backend.Models.MarketData;

namespace Backend.Models.Portfolio;

public class StrategyTradeLink
{
    public Guid Id { get; set; }

    public Guid TradeId { get; set; }
    public PortfolioTrade Trade { get; set; } = null!;

    public int StrategyExecutionId { get; set; }
    public StrategyExecution StrategyExecution { get; set; } = null!;
}

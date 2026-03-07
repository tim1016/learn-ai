using Backend.Models.MarketData;

namespace Backend.Models.Portfolio;

public class StrategyAllocation
{
    public Guid Id { get; set; }

    public Guid AccountId { get; set; }
    public Account Account { get; set; } = null!;

    public int StrategyExecutionId { get; set; }
    public StrategyExecution StrategyExecution { get; set; } = null!;

    public decimal CapitalAllocated { get; set; }
    public DateTime StartDate { get; set; }
    public DateTime? EndDate { get; set; }
}

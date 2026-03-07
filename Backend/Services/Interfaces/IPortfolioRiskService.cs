using Backend.Models.Portfolio;

namespace Backend.Services.Interfaces;

public interface IPortfolioRiskService
{
    Task<List<DollarDeltaResult>> ComputeDollarDeltaAsync(Guid accountId,
        Dictionary<string, decimal> prices, CancellationToken ct = default);
    Task<decimal> ComputePortfolioVegaAsync(Guid accountId, CancellationToken ct = default);
    Task<List<RiskViolation>> EvaluateRiskRulesAsync(Guid accountId,
        Dictionary<string, decimal> prices, CancellationToken ct = default);
    Task<ScenarioResult> RunScenarioAsync(Guid accountId,
        Dictionary<string, decimal> prices, ScenarioInput scenario, CancellationToken ct = default);
}

public class DollarDeltaResult
{
    public Guid PositionId { get; set; }
    public string Symbol { get; set; } = "";
    public decimal Delta { get; set; }
    public decimal Price { get; set; }
    public decimal Quantity { get; set; }
    public int Multiplier { get; set; }
    public decimal DollarDelta { get; set; }
}

public class RiskViolation
{
    public Guid RuleId { get; set; }
    public RiskRuleType RuleType { get; set; }
    public RiskAction Action { get; set; }
    public RiskSeverity Severity { get; set; }
    public decimal Threshold { get; set; }
    public decimal ActualValue { get; set; }
    public string Message { get; set; } = "";
}

public class ScenarioInput
{
    public decimal? PriceChangePercent { get; set; }
    public decimal? IvChangePercent { get; set; }
    public int? TimeDaysForward { get; set; }
}

public class ScenarioResult
{
    public decimal CurrentEquity { get; set; }
    public decimal ScenarioEquity { get; set; }
    public decimal PnLImpact { get; set; }
    public decimal PnLImpactPercent { get; set; }
    public List<PositionScenario> Positions { get; set; } = [];
}

public class PositionScenario
{
    public string Symbol { get; set; } = "";
    public decimal CurrentValue { get; set; }
    public decimal ScenarioValue { get; set; }
    public decimal PnLImpact { get; set; }
}

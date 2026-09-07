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

/// <summary>
/// A what-if shock. Despite the property names, <see cref="PriceChangePercent"/> and
/// <see cref="IvChangePercent"/> are <b>fractional</b> moves (-0.10 is a 10% drop): the
/// risk service forwards them unchanged as the Python scenario engine's spot and IV
/// shocks, and the Scenario Explorer divides its percent inputs by 100 before calling
/// the mutation. Callers holding a percent go through <see cref="FromPercent"/>.
/// </summary>
public class ScenarioInput
{
    public decimal? PriceChangePercent { get; set; }
    public decimal? IvChangePercent { get; set; }
    public int? TimeDaysForward { get; set; }

    /// <summary>Build a shock from percent figures (-10 for a 10% drop), converting to the fractions the engine expects.</summary>
    public static ScenarioInput FromPercent(decimal? priceChangePercent, decimal? ivChangePercent = null, int? timeDaysForward = null) =>
        new()
        {
            PriceChangePercent = priceChangePercent / 100m,
            IvChangePercent = ivChangePercent / 100m,
            TimeDaysForward = timeDaysForward,
        };
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

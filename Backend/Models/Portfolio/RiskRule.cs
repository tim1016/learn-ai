namespace Backend.Models.Portfolio;

public class RiskRule
{
    public Guid Id { get; set; }

    public Guid AccountId { get; set; }
    public Account Account { get; set; } = null!;

    public RiskRuleType RuleType { get; set; }
    public decimal Threshold { get; set; }
    public RiskAction Action { get; set; }
    public RiskSeverity Severity { get; set; }

    public bool Enabled { get; set; } = true;
    public DateTime? LastTriggered { get; set; }
}

public enum RiskRuleType
{
    MaxDrawdown,
    MaxPositionSize,
    MaxVegaExposure,
    MaxDelta
}

public enum RiskAction
{
    Warn,
    Block
}

public enum RiskSeverity
{
    Low,
    Medium,
    High,
    Critical
}

namespace Backend.GraphQL.Types;

/// <summary>
/// PR B (2026-05-19) — backend-neutral engine identity. The unified history
/// table groups runs by ``Engine`` regardless of which orchestrator produced
/// them. Derived from <see cref="Backend.Models.MarketData.StrategyExecution.Source"/>:
/// <list type="bullet">
///   <item><description><c>"engine"</c> → <see cref="PYTHON"/></description></item>
///   <item><description><c>"lean-sidecar"</c> → <see cref="LEAN"/></description></item>
/// </list>
/// Legacy <c>"strategy-lab"</c> rows are surfaced as <see cref="PYTHON"/> because
/// the legacy strategy-lab path was a Python engine variant before the unified
/// path existed. The <see cref="EngineSource"/> enum (separate file) still
/// covers all three database string values for the <c>engine</c> filter
/// argument.
/// </summary>
public enum Engine
{
    PYTHON,
    LEAN,
}

public static class EngineExtensions
{
    /// <summary>Map a <see cref="StrategyExecution.Source"/> string to the GraphQL <see cref="Engine"/>.</summary>
    public static Engine FromSource(string source) => source switch
    {
        "lean-sidecar" => Engine.LEAN,
        _ => Engine.PYTHON, // "engine", "strategy-lab", or unknown legacy → PYTHON
    };
}

using Backend.GraphQL.Types;
using Backend.Models.MarketData;
using HotChocolate;
using HotChocolate.Types;

namespace Backend.GraphQL.Resolvers;

/// <summary>
/// PR B (2026-05-19) — derived-field resolver that extends
/// <see cref="StrategyExecution"/> with the GraphQL fields the unified history
/// surface needs: <c>engine</c> (derived from <see cref="StrategyExecution.Source"/>)
/// and <c>dataPolicy</c> (parsed from the persisted
/// <see cref="StrategyExecution.DataPolicyJson"/> jsonb blob).
///
/// The original columns (<c>CommissionPerOrder</c>, <c>BrokeragePolicy</c>) are
/// exposed verbatim by HC's convention-based inference; we don't need explicit
/// resolvers for those — the database type and the GraphQL surface are the
/// same shape. Only the two derived fields live here.
/// </summary>
[ExtendObjectType<StrategyExecution>]
public class BacktestRunResolver
{
    /// <summary>
    /// PR B engine identity. Maps the legacy <see cref="StrategyExecution.Source"/>
    /// string (which can be <c>"engine"</c>, <c>"strategy-lab"</c>, or
    /// <c>"lean-sidecar"</c>) onto the unified two-state <see cref="Engine"/>
    /// enum that the v1 compare-view operates on.
    /// </summary>
    [GraphQLName("engine")]
    public Engine GetEngine([Parent] StrategyExecution execution)
        => EngineExtensions.FromSource(execution.Source);

    /// <summary>
    /// PR B canonical <see cref="DataPolicyType"/> block parsed from the
    /// persisted <see cref="StrategyExecution.DataPolicyJson"/> jsonb column.
    /// Null for legacy rows (predating the column) and for any malformed
    /// payload — corrupt rows surface as missing data instead of erroring the
    /// whole query.
    /// </summary>
    [GraphQLName("dataPolicy")]
    public DataPolicyType? GetDataPolicy([Parent] StrategyExecution execution)
        => DataPolicyType.TryParse(execution.DataPolicyJson);
}

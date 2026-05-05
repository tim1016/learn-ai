using Backend.GraphQL.Types;
using Backend.Models.DTOs;
using Backend.Services.Interfaces;
using HotChocolate;
using HotChocolate.Types;

namespace Backend.GraphQL;

/// <summary>
/// Spec-driven strategy mutations — type extension on the root Mutation.
/// Forwards calls to PythonDataService /api/spec-strategy/* via
/// <see cref="ISpecStrategyService"/>.
/// </summary>
[ExtendObjectType(typeof(Mutation))]
public class SpecStrategyMutation
{
    /// <summary>
    /// Run a backtest from a serialized StrategySpec.
    ///
    /// The <paramref name="specJson"/> is a JSON string carrying the
    /// validated spec — Frontend constructs it from form input + the
    /// JSON Schema export at GET /api/spec-strategy/schema. Backend
    /// passes it through to Python without re-validating; Python's
    /// Pydantic schema is the single source of truth for the spec
    /// shape.
    /// </summary>
    [GraphQLName("runSpecStrategyBacktest")]
    public async Task<SpecStrategyBacktestResultType> RunSpecStrategyBacktest(
        [Service] ISpecStrategyService specStrategyService,
        string specJson,
        string startDate,
        string endDate,
        decimal initialCash = 100000m,
        string fillMode = "signal_bar_close",
        decimal commissionPerOrder = 0m,
        CancellationToken cancellationToken = default)
    {
        try
        {
            var request = new SpecBacktestRequestDto(
                Spec: specJson,
                StartDate: startDate,
                EndDate: endDate,
                InitialCash: initialCash,
                FillMode: fillMode,
                CommissionPerOrder: commissionPerOrder
            );

            var result = await specStrategyService.RunBacktestAsync(request, cancellationToken);

            return SpecStrategyBacktestResultType.FromDto(result);
        }
        catch (Exception ex)
        {
            return new SpecStrategyBacktestResultType
            {
                Success = false,
                Error = ex.Message,
                Trades = [],
                LogLines = [],
            };
        }
    }
}

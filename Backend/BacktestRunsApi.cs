using Backend.Models.MarketData;
using Backend.Services.Interfaces;

namespace Backend;

/// <summary>
/// Minimal API endpoints for persisting LEAN sidecar backtest runs.
/// POST /api/backtest-runs/persist-lean — receives a serialized LEAN run
/// payload from PythonDataService and writes StrategyExecution + BacktestTrade rows.
/// </summary>
public static class BacktestRunsApi
{
    public static void MapBacktestRunsEndpoints(this WebApplication app)
    {
        var group = app.MapGroup("/api/backtest-runs").WithTags("BacktestRuns");

        group.MapPost("/persist-lean", PersistLeanRunAsync);
    }

    private static async Task<IResult> PersistLeanRunAsync(
        PersistLeanRunPayload payload,
        IBacktestRunPersistenceService service,
        CancellationToken ct)
    {
        try
        {
            var id = await service.PersistAsync(payload, ct);
            return Results.Ok(new { strategy_execution_id = id });
        }
        catch (ArgumentException ex)
        {
            return Results.BadRequest(new { error = ex.Message });
        }
    }
}

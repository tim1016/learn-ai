using Backend.Models.DTOs;
using Backend.Services.Interfaces;

namespace Backend;

/// <summary>
/// Minimal API endpoint for Recency Chart run persistence. Called by
/// PythonDataService's <c>persist_client.py</c> — the Recency Chart's sole
/// persistence path (never the best-effort engine study auto-save).
/// </summary>
public static class RecencyApi
{
    public static void MapRecencyEndpoints(this WebApplication app)
    {
        var group = app.MapGroup("/api/recency").WithTags("Recency");

        group.MapPost("/snapshots", PersistSnapshotAsync);
        group.MapPut("/launches/{launchId}/status", UpdateLaunchStatusAsync);
    }

    // ── POST /api/recency/snapshots — persist one run's trade snapshot ──
    private static async Task<IResult> PersistSnapshotAsync(
        RecencySnapshotRequest request,
        IRecencyPersistenceService persistence,
        CancellationToken ct)
    {
        var validationError = ValidateSnapshot(request);
        if (validationError is not null)
            return Results.BadRequest(new { error = validationError });

        try
        {
            var result = await persistence.PersistSnapshotAsync(request, ct);
            if (result.Skipped)
            {
                return Results.Ok(new { recency_run_id = (int?)null, skipped = true });
            }
            return Results.Ok(new { recency_run_id = result.RecencyRunId, skipped = false });
        }
        catch (InvalidOperationException ex)
        {
            return Results.NotFound(new { error = ex.Message });
        }
    }

    private static async Task<IResult> UpdateLaunchStatusAsync(
        string launchId,
        RecencyLaunchStatusRequest request,
        IRecencyLaunchService launches,
        CancellationToken ct)
    {
        try
        {
            var found = await launches.SetTerminalStatusAsync(
                launchId,
                request.Status,
                request.SucceededRuns,
                request.FailedRuns,
                ct);
            return found ? Results.NoContent() : Results.NotFound(new { error = $"RecencyLaunch {launchId} not found" });
        }
        catch (ArgumentException ex)
        {
            return Results.BadRequest(new { error = ex.Message });
        }
    }

    private static string? ValidateSnapshot(RecencySnapshotRequest request)
    {
        if (string.IsNullOrWhiteSpace(request.LaunchId) || string.IsNullOrWhiteSpace(request.Symbol)
            || string.IsNullOrWhiteSpace(request.StrategyKey) || string.IsNullOrWhiteSpace(request.ParamsHash))
            return "launch_id, symbol, strategy_key, and params_hash are required";
        if (request.Params is null || request.Trades is null)
            return "params and trades are required";

        foreach (var trade in request.Trades)
        {
            if (string.IsNullOrWhiteSpace(trade.Fingerprint))
                return "trade fingerprint is required";
            if (trade.EntryMs < 0 || trade.ExitMs < trade.EntryMs)
                return "trade timestamps must satisfy 0 <= entry_ms <= exit_ms";
            if (trade.Quantity <= 0)
                return "trade quantity must be positive";
            if (trade.HoldingSessions < 0)
                return "holding_sessions cannot be negative";
        }
        return null;
    }
}

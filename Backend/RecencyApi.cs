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
    }

    // ── POST /api/recency/snapshots — persist one run's trade snapshot ──
    private static async Task<IResult> PersistSnapshotAsync(
        RecencySnapshotRequest request,
        IRecencyPersistenceService persistence,
        CancellationToken ct)
    {
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
}

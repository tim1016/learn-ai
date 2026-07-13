using System.Text.Json;
using Backend.Data;
using Backend.Models.MarketData;
using Microsoft.EntityFrameworkCore;

namespace Backend;

/// <summary>
/// Internal REST surface the Python data plane uses to drive the
/// ParityVerdict state machine:
///
/// <list type="bullet">
///   <item><description><c>POST /api/parity-verdicts</c> — record the run-time
///   disposition (<c>pending</c> when a LEAN companion was dispatched,
///   <c>unavailable</c> with a reason otherwise). Idempotent per
///   parity group.</description></item>
///   <item><description><c>POST /api/parity-verdicts/{group}/mark-failed</c> —
///   companion failure surfacing (<c>run_failed</c> / <c>persist_failed</c>).
///   Conditional: only a <c>pending</c> row transitions; a verdict the
///   persist step already froze is never overwritten.</description></item>
/// </list>
///
/// Reads go through GraphQL (<c>backtestRun.parityVerdicts</c>); this
/// API is write-only.
/// </summary>
public static class ParityVerdictsApi
{
    private static readonly HashSet<string> CreateStatuses = new(StringComparer.Ordinal)
    {
        "pending",
        "unavailable",
    };

    private static readonly HashSet<string> FailureStatuses = new(StringComparer.Ordinal)
    {
        "run_failed",
        "persist_failed",
    };

    public static void MapParityVerdictsEndpoints(this WebApplication app)
    {
        var group = app.MapGroup("/api/parity-verdicts").WithTags("ParityVerdicts");
        group.MapPost("/", CreateAsync);
        group.MapPost("/{parityGroupId}/mark-failed", MarkFailedAsync);
    }

    private static async Task<IResult> CreateAsync(
        CreateParityVerdictRequest request,
        AppDbContext db,
        ILoggerFactory loggerFactory,
        CancellationToken ct)
    {
        var logger = loggerFactory.CreateLogger("ParityVerdictsApi");
        if (string.IsNullOrWhiteSpace(request.ParityGroupId))
            return Results.BadRequest(new { error = "parityGroupId is required" });
        if (!CreateStatuses.Contains(request.Status))
            return Results.BadRequest(new { error = $"status must be one of {string.Join('|', CreateStatuses)}" });

        var existing = await db.ParityVerdicts
            .AsNoTracking()
            .FirstOrDefaultAsync(v => v.ParityGroupId == request.ParityGroupId, ct);
        if (existing is not null)
        {
            // Idempotent: the group already has a disposition (possibly a
            // terminal verdict if the companion raced ahead) — keep it.
            return Results.Ok(new { id = existing.Id, status = existing.Status });
        }

        var leftExists = await db.StrategyExecutions.AnyAsync(e => e.Id == request.LeftExecutionId, ct);
        if (!leftExists)
            return Results.BadRequest(new { error = $"leftExecutionId {request.LeftExecutionId} does not exist" });

        var row = new ParityVerdict
        {
            LeftExecutionId = request.LeftExecutionId,
            RightExecutionId = null,
            ParityGroupId = request.ParityGroupId,
            VerdictVersion = Services.Implementation.ParityVerdictService.VerdictVersion,
            Status = request.Status,
            VerdictJson = string.IsNullOrWhiteSpace(request.VerdictJson) ? "{}" : request.VerdictJson,
            // CreatedAtUtc is a DateTime column (no migration needed); the model default
            // DateTime.UtcNow is overridden here with the authoritative value from
            // DateTimeOffset to avoid Local-kind ambiguity (temporal-rigor.md).
            CreatedAtUtc = DateTimeOffset.UtcNow.UtcDateTime,
        };
        db.ParityVerdicts.Add(row);
        try
        {
            await db.SaveChangesAsync(ct);
        }
        catch (DbUpdateException ex) when (ex.InnerException is Npgsql.PostgresException { SqlState: "23505" })
        {
            // Concurrent create for the same group — return the winner.
            var winner = await db.ParityVerdicts
                .AsNoTracking()
                .FirstAsync(v => v.ParityGroupId == request.ParityGroupId, ct);
            return Results.Ok(new { id = winner.Id, status = winner.Status });
        }

        logger.LogInformation(
            "[PARITY] Recorded {Status} disposition for group {Group} (left={LeftId})",
            request.Status, request.ParityGroupId, request.LeftExecutionId);
        return Results.Ok(new { id = row.Id, status = row.Status });
    }

    private static async Task<IResult> MarkFailedAsync(
        string parityGroupId,
        MarkParityFailedRequest request,
        AppDbContext db,
        ILoggerFactory loggerFactory,
        CancellationToken ct)
    {
        var logger = loggerFactory.CreateLogger("ParityVerdictsApi");
        if (!FailureStatuses.Contains(request.Status))
            return Results.BadRequest(new { error = $"status must be one of {string.Join('|', FailureStatuses)}" });

        // Atomic conditional update: only transitions if Status is still "pending".
        // ExecuteUpdateAsync translates to a single UPDATE … WHERE Status = 'pending',
        // preventing two concurrent requests from both winning the transition.
        var newVerdictJson = JsonSerializer.Serialize(new
        {
            schema_version = 1,
            parity_group_id = parityGroupId,
            status = request.Status,
            reason = request.Detail,
            computed_at_ms = DateTimeOffset.UtcNow.ToUnixTimeMilliseconds(),
        });
        var rowsAffected = await db.ParityVerdicts
            .Where(v => v.ParityGroupId == parityGroupId && v.Status == "pending")
            .ExecuteUpdateAsync(s => s
                .SetProperty(v => v.Status, request.Status)
                .SetProperty(v => v.VerdictJson, newVerdictJson),
                ct);

        if (rowsAffected == 0)
        {
            // Either the group doesn't exist or it's already terminal — first
            // terminal state wins; reload to return the winner's state.
            var existing = await db.ParityVerdicts
                .AsNoTracking()
                .FirstOrDefaultAsync(v => v.ParityGroupId == parityGroupId, ct);
            if (existing is null)
                return Results.NotFound(new { error = $"no parity verdict for group '{parityGroupId}'" });
            return Results.Ok(new { id = existing.Id, status = existing.Status, transitioned = false });
        }

        var updated = await db.ParityVerdicts
            .AsNoTracking()
            .FirstAsync(v => v.ParityGroupId == parityGroupId, ct);
        logger.LogInformation(
            "[PARITY] Group {Group} marked {Status}: {Detail}",
            parityGroupId, request.Status, request.Detail);
        return Results.Ok(new { id = updated.Id, status = updated.Status, transitioned = true });
    }
}

public record CreateParityVerdictRequest(
    string ParityGroupId,
    int LeftExecutionId,
    string Status,
    string? VerdictJson);

public record MarkParityFailedRequest(string Status, string Detail);

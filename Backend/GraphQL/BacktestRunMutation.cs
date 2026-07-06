using Backend.Data;
using HotChocolate;
using HotChocolate.Types;
using Microsoft.EntityFrameworkCore;

namespace Backend.GraphQL;

/// <summary>
/// PR B.3 (2026-05-19) — mutations on <c>BacktestRun</c> rows. Today this only
/// hosts <c>updateBacktestRunNotes</c>, which ports the REST notes-edit feature
/// from the soon-deleted <c>EngineHistoryComponent</c> into the GraphQL surface
/// so the unified history table can edit notes inline without touching
/// <c>/api/studies/{id}/notes</c> (which stays alive but no longer drives
/// the UI).
/// </summary>
[ExtendObjectType<Mutation>]
public class BacktestRunMutation
{
    /// <summary>
    /// Update the free-text notes column on a backtest run.
    /// Returns the updated row so the client can show the persisted value
    /// without a follow-up query. Throws <see cref="GraphQLException"/> when
    /// the id does not resolve to a row — surfaces as a structured GraphQL
    /// error rather than a 404, matching the rest of the schema.
    /// </summary>
    [GraphQLName("updateBacktestRunNotes")]
    public async Task<BacktestRunNotesResult> UpdateBacktestRunNotesAsync(
        AppDbContext db,
        int id,
        string notes,
        CancellationToken ct)
    {
        var execution = await db.StrategyExecutions.FirstOrDefaultAsync(s => s.Id == id, ct);
        if (execution is null)
        {
            throw new GraphQLException(
                ErrorBuilder.New()
                    .SetMessage($"BacktestRun {id} not found")
                    .SetCode("BACKTEST_RUN_NOT_FOUND")
                    .Build());
        }

        execution.Notes = notes;
        await db.SaveChangesAsync(ct);
        return new BacktestRunNotesResult(execution.Id, execution.Notes);
    }
}

public sealed record BacktestRunNotesResult(int Id, string? Notes);

using Backend.GraphQL;
using Backend.Models.MarketData;
using Backend.Tests.Helpers;
using Microsoft.EntityFrameworkCore;

namespace Backend.Tests.Unit.GraphQL;

/// <summary>
/// Tests for the PR B.3 GraphQL mutation that ports forward the REST notes-edit
/// feature from the soon-deleted ``EngineHistoryComponent``. Verifies the
/// happy-path write, the not-found behaviour, and that the persisted notes
/// survive a re-read.
/// </summary>
public class BacktestRunMutationTests
{
    private static async Task<(BacktestRunMutation mutation, Backend.Data.AppDbContext db, int existingId)> BuildAsync()
    {
        var db = TestDbContextFactory.Create();

        var ticker = new Ticker { Symbol = "SPY", Name = "SPDR S&P 500", Market = "stocks" };
        db.Tickers.Add(ticker);
        await db.SaveChangesAsync();

        var execution = new StrategyExecution
        {
            TickerId = ticker.Id,
            StrategyName = "EMA Crossover",
            Source = "engine",
            Parameters = "{\"symbol\":\"SPY\"}",
            StartDate = "2025-01-01",
            EndDate = "2025-01-31",
            Notes = "initial note",
        };
        db.StrategyExecutions.Add(execution);
        await db.SaveChangesAsync();

        return (new BacktestRunMutation(), db, execution.Id);
    }

    [Fact]
    public async Task UpdateBacktestRunNotes_ExistingId_PersistsNewNotes()
    {
        var (mutation, db, id) = await BuildAsync();

        var result = await mutation.UpdateBacktestRunNotesAsync(db, id, "updated note", CancellationToken.None);

        Assert.NotNull(result);
        Assert.Equal(id, result!.Id);
        Assert.Equal("updated note", result.Notes);

        // Re-read confirms the change actually committed.
        var reread = await db.StrategyExecutions.AsNoTracking().FirstAsync(s => s.Id == id);
        Assert.Equal("updated note", reread.Notes);
    }

    [Fact]
    public async Task UpdateBacktestRunNotes_EmptyString_Persists()
    {
        // Clearing notes is the standard UX for removing a comment; the mutation
        // must accept "" (not just null) so the inline editor's "save empty" path
        // works without a separate "delete notes" call.
        var (mutation, db, id) = await BuildAsync();

        var result = await mutation.UpdateBacktestRunNotesAsync(db, id, "", CancellationToken.None);

        Assert.NotNull(result);
        Assert.Equal("", result!.Notes);
    }

    [Fact]
    public async Task UpdateBacktestRunNotes_MissingId_ThrowsGraphQLException()
    {
        var (mutation, db, _) = await BuildAsync();

        await Assert.ThrowsAsync<HotChocolate.GraphQLException>(
            () => mutation.UpdateBacktestRunNotesAsync(db, id: 999_999, notes: "x", CancellationToken.None));
    }
}

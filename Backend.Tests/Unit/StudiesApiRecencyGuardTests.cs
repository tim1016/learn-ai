using Backend.Tests.Helpers;
using Npgsql;

namespace Backend.Tests.Unit;

/// <summary>
/// Hard-delete guard (design spec D22, P0-4): a study backing a live
/// (non-tombstoned) RecencyRun must not be deletable via
/// DELETE /api/studies/{id} — deleting it out from under the Recency
/// Chart would break "forever until you soft-delete it" (D17).
///
/// The Recency tables are owned by the Python service (ADR 0057) and the
/// guard reads them by name, so this is a PostgreSQL test: the migrated
/// schema still holds the tables (the handover migration is a no-op) and the
/// rows are seeded with the SQL the Python writer uses.
/// </summary>
public class StudiesApiRecencyGuardTests
{
    [Fact]
    public async Task IsRecencyMemberAsync_StudyBackingALiveRecencyRun_ReturnsTrue()
    {
        await using var database = await PostgresIntegrationTestDatabase.CreateMigratedAsync();
        await SeedRunAsync(database.ConnectionString, studyId: 42, deletedAtMs: null);
        await using var db = database.CreateContext("recency-guard-live");

        var result = await StudiesApi.IsRecencyMemberAsync(db, 42, CancellationToken.None);

        Assert.True(result);
    }

    [Fact]
    public async Task IsRecencyMemberAsync_StudyNotReferencedByAnyRecencyRun_ReturnsFalse()
    {
        await using var database = await PostgresIntegrationTestDatabase.CreateMigratedAsync();
        await using var db = database.CreateContext("recency-guard-none");

        var result = await StudiesApi.IsRecencyMemberAsync(db, 99, CancellationToken.None);

        Assert.False(result);
    }

    [Fact]
    public async Task IsRecencyMemberAsync_OnlyReferencedBySoftDeletedRun_ReturnsFalse()
    {
        await using var database = await PostgresIntegrationTestDatabase.CreateMigratedAsync();
        await SeedRunAsync(database.ConnectionString, studyId: 42, deletedAtMs: 500);
        await using var db = database.CreateContext("recency-guard-tombstoned");

        // A study whose ONLY recency membership was already soft-deleted is
        // no longer "live" — its hard-delete guard does not need to block.
        var result = await StudiesApi.IsRecencyMemberAsync(db, 42, CancellationToken.None);

        Assert.False(result);
    }

    private static async Task SeedRunAsync(string connectionString, int studyId, long? deletedAtMs)
    {
        await using var connection = new NpgsqlConnection(connectionString);
        await connection.OpenAsync();
        await using var launch = new NpgsqlCommand(
            """
            INSERT INTO "RecencyLaunches" ("Id", "ConfigJson", "ExpectedRuns", "SucceededRuns", "FailedRuns", "Status", "CreatedAtMs")
            VALUES ('l1', '{}'::jsonb, 1, 1, 0, 'RUNNING', 1)
            """,
            connection);
        await launch.ExecuteNonQueryAsync();
        await using var run = new NpgsqlCommand(
            """
            INSERT INTO "RecencyRuns" ("RecencyLaunchId", "Symbol", "StrategyKey", "ParamsJson", "ParamsHash", "StudyId", "TotalPnl", "CreatedAtMs", "DeletedAtMs")
            VALUES ('l1', 'SPY', 'ema_crossover_2_bps', '{}'::jsonb, 'h1', @studyId, 10, 1, @deletedAtMs)
            """,
            connection);
        run.Parameters.AddWithValue("studyId", studyId);
        run.Parameters.AddWithValue("deletedAtMs", (object?)deletedAtMs ?? DBNull.Value);
        await run.ExecuteNonQueryAsync();
    }
}

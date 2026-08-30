using Backend.Data;
using Backend.Models.MarketData;
using Backend.Tests.Helpers;
using Microsoft.EntityFrameworkCore;
using Microsoft.EntityFrameworkCore.Infrastructure;
using Microsoft.EntityFrameworkCore.Migrations;
using Microsoft.Extensions.DependencyInjection;
using Npgsql;

namespace Backend.Tests.Data;

/// <summary>
/// Migration-specific coverage for 20260830120000_ActivateDataRootScopedCatalogIdentity
/// (issue #1878, PR B of #1861). SchemaMigrationTests.cs's full-migration
/// fingerprint test proves this migration's raw-SQL objects exist after a
/// fresh migrate; this file proves the two properties that test cannot:
/// the Down() migration's data-safety refusal, and the actual column order
/// of the rebuilt partial unique indexes (their name existing does not
/// prove DataRootId leads).
/// </summary>
public class ActivateDataRootScopedCatalogIdentityMigrationTests
{
    private const string PreviousMigration = "20260829120000_AddDataRootIdToDataLakeArtifactsAndRuns";
    private const string ThisMigration = "20260830120000_ActivateDataRootScopedCatalogIdentity";

    [Fact]
    [Trait("Category", "PostgresIntegration")]
    public async Task Up_DropsLegacyConstraintAndDataRootIdDefaults()
    {
        await using var database = await PostgresIntegrationTestDatabase.CreateMigratedAsync();
        await using var connection = new NpgsqlConnection(database.ConnectionString);
        await connection.OpenAsync();

        Assert.False(await ConstraintExistsAsync(connection, "ck_raw_only_for_canonical_data_root"));

        foreach (var table in new[] { "DataLakeArtifacts", "DataLakeRuns" })
        {
            await using var command = new NpgsqlCommand(
                "SELECT column_default FROM information_schema.columns " +
                "WHERE table_schema = 'public' AND table_name = @table AND column_name = 'DataRootId';",
                connection);
            command.Parameters.AddWithValue("table", table);
            var columnDefault = await command.ExecuteScalarAsync();

            Assert.True(columnDefault is null or DBNull, $"{table}.DataRootId still has a server default: {columnDefault}");
        }
    }

    [Fact]
    [Trait("Category", "PostgresIntegration")]
    public async Task Up_RebuildsPartialIndexesWithDataRootIdLeading()
    {
        await using var database = await PostgresIntegrationTestDatabase.CreateMigratedAsync();
        await using var connection = new NpgsqlConnection(database.ConnectionString);
        await connection.OpenAsync();

        var expectedLeadingColumn = new Dictionary<string, string>
        {
            ["uq_data_lake_artifacts_minute_bars"] = "\"DataRootId\"",
            ["uq_data_lake_artifacts_aggregated_bars"] = "\"DataRootId\"",
            ["uq_data_lake_artifacts_corp_actions"] = "\"DataRootId\"",
            ["uq_data_lake_artifacts_metadata"] = "\"DataRootId\"",
            ["ix_data_lake_artifacts_root_scoped_coverage"] = "\"DataRootId\"",
        };

        foreach (var (indexName, expectedLeadingColumnName) in expectedLeadingColumn)
        {
            await using var command = new NpgsqlCommand(
                "SELECT pg_get_indexdef(indexrelid) FROM pg_index " +
                "WHERE indexrelid::regclass::text = @indexName;",
                connection);
            command.Parameters.AddWithValue("indexName", indexName);
            var indexDef = (string?)await command.ExecuteScalarAsync();

            Assert.NotNull(indexDef);
            var openParen = indexDef!.IndexOf('(');
            var closeParen = indexDef.IndexOf(')');
            var columnList = indexDef[(openParen + 1)..closeParen];
            var firstColumn = columnList.Split(',')[0].Trim();

            Assert.Equal(expectedLeadingColumnName, firstColumn);
        }
    }

    [Fact]
    [Trait("Category", "PostgresIntegration")]
    public async Task Down_NoConflicts_RestoresLegacySchema()
    {
        await using var database = await PostgresIntegrationTestDatabase.CreateMigratedAsync();
        using var services = new ServiceCollection()
            .AddDbContext<AppDbContext>(options => options.UseNpgsql(database.ConnectionString))
            .BuildServiceProvider();
        await using var scope = services.CreateAsyncScope();
        var migrator = scope.ServiceProvider.GetRequiredService<AppDbContext>().GetService<IMigrator>();

        await migrator.MigrateAsync(PreviousMigration, CancellationToken.None);

        await using var connection = new NpgsqlConnection(database.ConnectionString);
        await connection.OpenAsync();
        Assert.True(await ConstraintExistsAsync(connection, "ck_raw_only_for_canonical_data_root"));
        Assert.False(await IsMigrationAppliedAsync(connection, ThisMigration));

        await using var command = new NpgsqlCommand(
            "SELECT pg_get_indexdef(indexrelid) FROM pg_index " +
            "WHERE indexrelid::regclass::text = 'uq_data_lake_artifacts_minute_bars';",
            connection);
        var indexDef = (string?)await command.ExecuteScalarAsync();
        Assert.NotNull(indexDef);
        Assert.DoesNotContain("\"DataRootId\"", indexDef);
    }

    [Fact]
    [Trait("Category", "PostgresIntegration")]
    public async Task Down_MinuteBarMultiRootConflict_RefusesAndLeavesSchemaUnchanged()
    {
        await using var database = await PostgresIntegrationTestDatabase.CreateMigratedAsync();
        using var services = new ServiceCollection()
            .AddDbContext<AppDbContext>(options => options.UseNpgsql(database.ConnectionString))
            .BuildServiceProvider();
        await using var scope = services.CreateAsyncScope();
        var context = scope.ServiceProvider.GetRequiredService<AppDbContext>();
        var migrator = context.GetService<IMigrator>();

        // Two minute-bar rows sharing every dimension of the OLD (mode-only)
        // unique key except DataRootId -- only possible post-migration,
        // because the rebuilt index leads with DataRootId.
        context.DataLakeArtifacts.AddRange(
            NewCompleteMinuteBar(Guid.NewGuid(), "x.zip"),
            NewCompleteMinuteBar(Guid.NewGuid(), "y.zip"));
        await context.SaveChangesAsync();

        var exception = await Assert.ThrowsAsync<PostgresException>(() =>
            migrator.MigrateAsync(PreviousMigration, CancellationToken.None));

        Assert.Contains("rollback refused", exception.MessageText, StringComparison.OrdinalIgnoreCase);
        Assert.Contains("minute-bar", exception.MessageText, StringComparison.OrdinalIgnoreCase);

        await using var connection = new NpgsqlConnection(database.ConnectionString);
        await connection.OpenAsync();
        // The failed Down() ran inside a transaction -- nothing collapsed.
        Assert.True(await IsMigrationAppliedAsync(connection, ThisMigration));
        Assert.False(await ConstraintExistsAsync(connection, "ck_raw_only_for_canonical_data_root"));
    }

    [Fact]
    [Trait("Category", "PostgresIntegration")]
    public async Task Down_MetadataMultiRootConflict_RefusesAndLeavesSchemaUnchanged()
    {
        await using var database = await PostgresIntegrationTestDatabase.CreateMigratedAsync();
        using var services = new ServiceCollection()
            .AddDbContext<AppDbContext>(options => options.UseNpgsql(database.ConnectionString))
            .BuildServiceProvider();
        await using var scope = services.CreateAsyncScope();
        var context = scope.ServiceProvider.GetRequiredService<AppDbContext>();
        var migrator = context.GetService<IMigrator>();

        // Two metadata rows sharing the OLD key (DataContractHash alone)
        // but different DataRootId.
        var sharedHash = new string('m', 64);
        context.DataLakeArtifacts.AddRange(
            NewCompleteMetadata(Guid.NewGuid(), sharedHash),
            NewCompleteMetadata(Guid.NewGuid(), sharedHash));
        await context.SaveChangesAsync();

        var exception = await Assert.ThrowsAsync<PostgresException>(() =>
            migrator.MigrateAsync(PreviousMigration, CancellationToken.None));

        Assert.Contains("rollback refused", exception.MessageText, StringComparison.OrdinalIgnoreCase);
        Assert.Contains("metadata", exception.MessageText, StringComparison.OrdinalIgnoreCase);
    }

    private static DataLakeArtifact NewCompleteMinuteBar(Guid dataRootId, string filePath) => new()
    {
        DataRootId = dataRootId,
        ArtifactKind = "time_series_bars",
        Market = "usa",
        Symbol = "SPY",
        TradingDate = new DateOnly(2024, 5, 20),
        Resolution = "minute",
        DataType = "trade",
        Provider = "polygon",
        ProviderParams = "{}",
        PriceAdjustmentMode = "raw",
        DataContractHash = new string('a', 64),
        FilePath = filePath,
        Status = "complete",
        AttemptCount = 1,
        FetchedAtMs = 0,
    };

    private static DataLakeArtifact NewCompleteMetadata(Guid dataRootId, string dataContractHash) => new()
    {
        DataRootId = dataRootId,
        ArtifactKind = "metadata",
        Provider = "lean_image_extract",
        ProviderParams = "{}",
        DataContractHash = dataContractHash,
        FilePath = "market-hours-database.json",
        Status = "complete",
        AttemptCount = 1,
        FetchedAtMs = 0,
    };

    private static async Task<bool> ConstraintExistsAsync(NpgsqlConnection connection, string constraintName)
    {
        await using var command = new NpgsqlCommand(
            "SELECT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = @constraintName);",
            connection);
        command.Parameters.AddWithValue("constraintName", constraintName);
        return (bool)(await command.ExecuteScalarAsync())!;
    }

    private static async Task<bool> IsMigrationAppliedAsync(NpgsqlConnection connection, string migrationId)
    {
        await using var command = new NpgsqlCommand(
            "SELECT EXISTS (SELECT 1 FROM \"__EFMigrationsHistory\" WHERE \"MigrationId\" = @migrationId);",
            connection);
        command.Parameters.AddWithValue("migrationId", migrationId);
        return (bool)(await command.ExecuteScalarAsync())!;
    }
}

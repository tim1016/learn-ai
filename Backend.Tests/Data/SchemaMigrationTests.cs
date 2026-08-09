using Backend.Data;
using Backend.Models.MarketData;
using Backend.Tests.Helpers;
using Microsoft.EntityFrameworkCore;
using Microsoft.EntityFrameworkCore.Infrastructure;
using Microsoft.EntityFrameworkCore.Migrations;
using Microsoft.Extensions.DependencyInjection;
using Microsoft.Extensions.Logging.Abstractions;
using Npgsql;
using System.Reflection;

namespace Backend.Tests.Data;

public class SchemaMigrationTests
{
    private const string MigrationBeforeLegacySchemaRepair = "20260717010000_PreserveUnavailableRunMetrics";

    [Fact]
    public void StrategyExecution_HasLeanRunIdProperty()
    {
        var prop = typeof(StrategyExecution).GetProperty(nameof(StrategyExecution.LeanRunId));
        Assert.NotNull(prop);
        Assert.Equal(typeof(string), prop!.PropertyType);
    }

    [Fact]
    public void BacktestTrade_HasIsSyntheticExitProperty()
    {
        var prop = typeof(BacktestTrade).GetProperty(nameof(BacktestTrade.IsSyntheticExit));
        Assert.NotNull(prop);
        Assert.Equal(typeof(bool), prop!.PropertyType);
    }

    [Fact]
    public void AllConcreteMigrations_AreDiscoverableByEf()
    {
        var options = new DbContextOptionsBuilder<AppDbContext>()
            .UseNpgsql("Host=localhost;Database=migration_discovery;Username=postgres;Password=postgres")
            .Options;

        using var context = new AppDbContext(options);
        var declaredMigrationIds = typeof(AppDbContext).Assembly
            .GetTypes()
            .Where(type => !type.IsAbstract && typeof(Migration).IsAssignableFrom(type))
            .Select(type => type.GetCustomAttribute<MigrationAttribute>()?.Id)
            .OrderBy(id => id)
            .ToArray();
        var discoveredMigrationIds = context.GetService<IMigrationsAssembly>()
            .Migrations
            .Keys
            .OrderBy(id => id)
            .ToArray();

        Assert.DoesNotContain(declaredMigrationIds, id => string.IsNullOrWhiteSpace(id));
        Assert.Equal(declaredMigrationIds, discoveredMigrationIds);
    }

    [Fact]
    public void ProductionStartup_UsesMigrationInitializer()
    {
        var programPath = FindRepositoryFile("Backend", "Program.cs");
        var startupSource = File.ReadAllText(programPath);

        Assert.Contains("DatabaseInitializer.MigrateAsync(", startupSource, StringComparison.Ordinal);
        Assert.DoesNotContain("Database.EnsureCreated", startupSource, StringComparison.Ordinal);
    }

    [Fact]
    public async Task DatabaseInitializer_TransientFailure_RetriesOnce()
    {
        var attempts = 0;

        await DatabaseInitializer.ExecuteWithRetryAsync(
            _ =>
            {
                attempts++;
                return attempts == 1
                    ? Task.FromException(new TimeoutException("PostgreSQL is temporarily unavailable."))
                    : Task.CompletedTask;
            },
            NullLogger.Instance,
            CancellationToken.None,
            TimeSpan.Zero);

        Assert.Equal(2, attempts);
    }

    [Fact]
    public async Task DatabaseInitializer_PermanentMigrationFailure_DoesNotRetry()
    {
        var attempts = 0;
        var expectedException = new PostgresException(
            "PortfolioSnapshots contains non-null Greek data; aborting destructive column drop",
            "ERROR",
            "ERROR",
            "P0001");

        var actualException = await Assert.ThrowsAsync<PostgresException>(() =>
            DatabaseInitializer.ExecuteWithRetryAsync(
                _ =>
                {
                    attempts++;
                    return Task.FromException(expectedException);
                },
                NullLogger.Instance,
                CancellationToken.None,
                TimeSpan.Zero));

        Assert.Same(expectedException, actualException);
        Assert.Equal(1, attempts);
    }

    [Fact]
    [Trait("Category", "PostgresIntegration")]
    public async Task DatabaseInitializer_FreshDatabase_AppliesEveryMigrationAndLeavesNonePending()
    {
        await using var database = await PostgresIntegrationTestDatabase.CreateAsync();
        using var services = new ServiceCollection()
            .AddDbContext<AppDbContext>(options => options.UseNpgsql(database.ConnectionString))
            .BuildServiceProvider();

        await DatabaseInitializer.MigrateAsync(services, NullLogger.Instance, CancellationToken.None);

        await using (var scope = services.CreateAsyncScope())
        {
            var context = scope.ServiceProvider.GetRequiredService<AppDbContext>();
            var expectedMigrationIds = context.Database.GetMigrations().OrderBy(id => id).ToArray();
            var appliedMigrationIds = (await context.Database.GetAppliedMigrationsAsync()).OrderBy(id => id).ToArray();
            var pendingMigrationIds = await context.Database.GetPendingMigrationsAsync();

            Assert.Equal(expectedMigrationIds, appliedMigrationIds);
            Assert.Empty(pendingMigrationIds);
        }

        await DatabaseInitializer.MigrateAsync(services, NullLogger.Instance, CancellationToken.None);

        await using var connection = new NpgsqlConnection(database.ConnectionString);
        await connection.OpenAsync();

        foreach (var constraintName in RawSqlMigrationConstraints)
        {
            await using var command = new NpgsqlCommand(
                "SELECT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = @constraintName);",
                connection);
            command.Parameters.AddWithValue("constraintName", constraintName);

            Assert.True((bool)(await command.ExecuteScalarAsync())!);
        }

        foreach (var indexName in RawSqlMigrationIndexes)
        {
            await AssertRelationExistsAsync(connection, $"public.{indexName}");
        }

        foreach (var tableName in RetiredLifecycleProjectionTables)
        {
            await AssertRelationDoesNotExistAsync(connection, $"public.{tableName}");
        }
    }

    [Fact]
    [Trait("Category", "PostgresIntegration")]
    public async Task RepairLegacySchemaDrift_RecreatesTheCanonicalRawSqlCatalog()
    {
        await using var cleanDatabase = await PostgresIntegrationTestDatabase.CreateAsync();
        await using var legacyDatabase = await PostgresIntegrationTestDatabase.CreateAsync();
        using var cleanServices = CreateServices(cleanDatabase.ConnectionString);
        using var legacyServices = CreateServices(legacyDatabase.ConnectionString);

        await DatabaseInitializer.MigrateAsync(cleanServices, NullLogger.Instance, CancellationToken.None);

        await using var legacyScope = legacyServices.CreateAsyncScope();
        var legacyContext = legacyScope.ServiceProvider.GetRequiredService<AppDbContext>();
        var legacyMigrator = legacyContext.GetService<IMigrator>();
        await legacyMigrator.MigrateAsync(MigrationBeforeLegacySchemaRepair, CancellationToken.None);

        await using (var legacyConnection = new NpgsqlConnection(legacyDatabase.ConnectionString))
        {
            await legacyConnection.OpenAsync();
            await RecreateLegacySchemaDriftAsync(legacyConnection);
        }

        await legacyMigrator.MigrateAsync(cancellationToken: CancellationToken.None);

        await using var cleanConnection = new NpgsqlConnection(cleanDatabase.ConnectionString);
        await cleanConnection.OpenAsync();
        await using var repairedLegacyConnection = new NpgsqlConnection(legacyDatabase.ConnectionString);
        await repairedLegacyConnection.OpenAsync();

        var cleanFingerprint = await GetRepairScopeFingerprintAsync(cleanConnection);
        var repairedLegacyFingerprint = await GetRepairScopeFingerprintAsync(repairedLegacyConnection);

        Assert.Equal(cleanFingerprint, repairedLegacyFingerprint);
    }

    [Fact]
    [Trait("Category", "PostgresIntegration")]
    public async Task ReconcileLegacySchemaRepairContract_UpdatesDatabasesThatAlreadyAppliedTheInitialRepair()
    {
        await using var cleanDatabase = await PostgresIntegrationTestDatabase.CreateAsync();
        await using var upgradedDatabase = await PostgresIntegrationTestDatabase.CreateAsync();
        using var cleanServices = CreateServices(cleanDatabase.ConnectionString);
        using var upgradedServices = CreateServices(upgradedDatabase.ConnectionString);

        await DatabaseInitializer.MigrateAsync(cleanServices, NullLogger.Instance, CancellationToken.None);

        await using var upgradedScope = upgradedServices.CreateAsyncScope();
        var upgradedContext = upgradedScope.ServiceProvider.GetRequiredService<AppDbContext>();
        var upgradedMigrator = upgradedContext.GetService<IMigrator>();
        await upgradedMigrator.MigrateAsync(RepairLegacySchemaDriftMigration, CancellationToken.None);

        await using (var upgradedConnection = new NpgsqlConnection(upgradedDatabase.ConnectionString))
        {
            await upgradedConnection.OpenAsync();
            await CorruptRepairedRawSqlCatalogAsync(upgradedConnection);
        }

        await upgradedMigrator.MigrateAsync(cancellationToken: CancellationToken.None);

        await using var cleanConnection = new NpgsqlConnection(cleanDatabase.ConnectionString);
        await cleanConnection.OpenAsync();
        await using var upgradedVerificationConnection = new NpgsqlConnection(upgradedDatabase.ConnectionString);
        await upgradedVerificationConnection.OpenAsync();

        Assert.Equal(
            await GetRepairScopeFingerprintAsync(cleanConnection),
            await GetRepairScopeFingerprintAsync(upgradedVerificationConnection));
        Assert.True(await IsMigrationAppliedAsync(
            upgradedVerificationConnection,
            ReconcileLegacySchemaRepairContractMigration));
    }

    [Fact]
    [Trait("Category", "PostgresIntegration")]
    public async Task RepairLegacySchemaDrift_PopulatedGreekColumns_AbortsDestructiveDrop()
    {
        await using var database = await PostgresIntegrationTestDatabase.CreateAsync();
        using var services = new ServiceCollection()
            .AddDbContext<AppDbContext>(options => options.UseNpgsql(database.ConnectionString))
            .BuildServiceProvider();
        await using var scope = services.CreateAsyncScope();
        var context = scope.ServiceProvider.GetRequiredService<AppDbContext>();
        var migrator = context.GetService<IMigrator>();

        await migrator.MigrateAsync(MigrationBeforeLegacySchemaRepair, CancellationToken.None);

        string[] fingerprintBeforeMigration;
        await using (var connection = new NpgsqlConnection(database.ConnectionString))
        {
            await connection.OpenAsync();

            await using (var addGreekColumns = new NpgsqlCommand(@"
                ALTER TABLE ""PortfolioSnapshots"" ADD COLUMN ""NetDelta"" numeric(18,8);
                ALTER TABLE ""PortfolioSnapshots"" ADD COLUMN ""NetGamma"" numeric(18,8);
                ALTER TABLE ""PortfolioSnapshots"" ADD COLUMN ""NetTheta"" numeric(18,8);
                ALTER TABLE ""PortfolioSnapshots"" ADD COLUMN ""NetVega"" numeric(18,8);", connection))
            {
                await addGreekColumns.ExecuteNonQueryAsync();
            }

            await using var seedGreekData = new NpgsqlCommand(@"
                INSERT INTO ""Accounts"" (""Id"", ""Name"", ""Type"", ""BaseCurrency"", ""InitialCash"", ""Cash"", ""CreatedAt"")
                VALUES (@accountId, 'migration guard', 'test', 'USD', 0, 0, @timestamp);

                INSERT INTO ""PortfolioSnapshots"" (""Id"", ""AccountId"", ""Timestamp"", ""Equity"", ""Cash"", ""MarketValue"", ""MarginUsed"", ""UnrealizedPnL"", ""RealizedPnL"", ""NetDelta"")
                VALUES (@snapshotId, @accountId, @timestamp, 0, 0, 0, 0, 0, 0, 1);", connection);
            seedGreekData.Parameters.AddWithValue("accountId", Guid.NewGuid());
            seedGreekData.Parameters.AddWithValue("snapshotId", Guid.NewGuid());
            seedGreekData.Parameters.AddWithValue("timestamp", DateTime.UtcNow);
            await seedGreekData.ExecuteNonQueryAsync();

            fingerprintBeforeMigration = await GetRepairScopeFingerprintAsync(connection);
        }

        var exception = await Assert.ThrowsAsync<PostgresException>(() =>
            migrator.MigrateAsync(cancellationToken: CancellationToken.None));

        Assert.Contains("PortfolioSnapshots contains non-null Greek data", exception.MessageText, StringComparison.Ordinal);

        await using var verificationConnection = new NpgsqlConnection(database.ConnectionString);
        await verificationConnection.OpenAsync();
        var fingerprintAfterFailure = await GetRepairScopeFingerprintAsync(verificationConnection);

        Assert.Equal(fingerprintBeforeMigration, fingerprintAfterFailure);
        Assert.False(await IsMigrationAppliedAsync(verificationConnection, RepairLegacySchemaDriftMigration));
    }

    [Fact]
    [Trait("Category", "PostgresIntegration")]
    public async Task AssertRelationExistsAsync_MissingRelation_Throws()
    {
        await using var database = await PostgresIntegrationTestDatabase.CreateAsync();
        await using var connection = new NpgsqlConnection(database.ConnectionString);
        await connection.OpenAsync();

        var exception = await Record.ExceptionAsync(() =>
            AssertRelationExistsAsync(connection, "public.missing_migration_relation"));

        Assert.NotNull(exception);
    }

    private static readonly string[] RetiredLifecycleProjectionTables =
    [
        "bot_lifecycle_events",
        "account_lifecycle_events",
        "operator_gate_snapshots",
        "lifecycle_node_receipts",
        "account_owner_status_snapshots"
    ];

    private static readonly string[] RawSqlMigrationConstraints =
    [
        "ck_artifact_kind_fields",
        "ck_artifact_kind_enum",
        "ck_resolution_enum",
        "ck_data_type_enum",
        "ck_price_adjustment_mode_enum",
        "ck_status_enum",
        "ck_raw_only_for_canonical_data_root",
        "ck_data_lake_runs_run_type",
        "ck_data_lake_runs_ensure_data_status",
        "ck_data_lake_runs_engine_status"
    ];

    private static readonly string[] RawSqlMigrationIndexes =
    [
        "ix_strategyexecution_datapolicy_symbol",
        "uq_data_lake_artifacts_minute_bars",
        "uq_data_lake_artifacts_aggregated_bars",
        "uq_data_lake_artifacts_corp_actions",
        "uq_data_lake_artifacts_metadata",
        "ix_data_lake_artifacts_corp_action_lookup",
        "ix_data_lake_artifacts_incomplete"
    ];

    private const string RepairLegacySchemaDriftMigration = "20260720010000_RepairLegacySchemaDrift";
    private const string ReconcileLegacySchemaRepairContractMigration = "20260720020000_ReconcileLegacySchemaRepairContract";

    private static ServiceProvider CreateServices(string connectionString) =>
        new ServiceCollection()
            .AddDbContext<AppDbContext>(options => options.UseNpgsql(connectionString))
            .BuildServiceProvider();

    private static async Task RecreateLegacySchemaDriftAsync(NpgsqlConnection connection)
    {
        await using var command = new NpgsqlCommand(@"
            DROP INDEX IF EXISTS ix_strategyexecution_datapolicy_symbol;
            CREATE INDEX ix_strategyexecution_datapolicy_symbol
              ON ""StrategyExecutions"" ((""DataPolicyJson""->>'market'));

            ALTER TABLE ""DataLakeArtifacts"" DROP CONSTRAINT IF EXISTS ck_artifact_kind_fields;
            ALTER TABLE ""DataLakeArtifacts"" DROP CONSTRAINT IF EXISTS ck_artifact_kind_enum;
            ALTER TABLE ""DataLakeArtifacts"" DROP CONSTRAINT IF EXISTS ck_resolution_enum;
            ALTER TABLE ""DataLakeArtifacts"" DROP CONSTRAINT IF EXISTS ck_data_type_enum;
            ALTER TABLE ""DataLakeArtifacts"" DROP CONSTRAINT IF EXISTS ck_price_adjustment_mode_enum;
            ALTER TABLE ""DataLakeArtifacts"" DROP CONSTRAINT IF EXISTS ck_status_enum;
            ALTER TABLE ""DataLakeArtifacts"" DROP CONSTRAINT IF EXISTS ck_raw_only_for_canonical_data_root;
            ALTER TABLE ""DataLakeRuns"" DROP CONSTRAINT IF EXISTS ck_data_lake_runs_run_type;
            ALTER TABLE ""DataLakeRuns"" DROP CONSTRAINT IF EXISTS ck_data_lake_runs_ensure_data_status;
            ALTER TABLE ""DataLakeRuns"" DROP CONSTRAINT IF EXISTS ck_data_lake_runs_engine_status;

            DROP INDEX IF EXISTS uq_data_lake_artifacts_minute_bars;
            DROP INDEX IF EXISTS uq_data_lake_artifacts_aggregated_bars;
            DROP INDEX IF EXISTS uq_data_lake_artifacts_corp_actions;
            DROP INDEX IF EXISTS uq_data_lake_artifacts_metadata;
            DROP INDEX IF EXISTS ix_data_lake_artifacts_corp_action_lookup;
            DROP INDEX IF EXISTS ix_data_lake_artifacts_incomplete;

            ALTER TABLE ""PortfolioSnapshots"" ADD COLUMN ""NetDelta"" numeric(18,8);
            ALTER TABLE ""PortfolioSnapshots"" ADD COLUMN ""NetGamma"" numeric(18,8);
            ALTER TABLE ""PortfolioSnapshots"" ADD COLUMN ""NetTheta"" numeric(18,8);
            ALTER TABLE ""PortfolioSnapshots"" ADD COLUMN ""NetVega"" numeric(18,8);", connection);

        await command.ExecuteNonQueryAsync();
    }

    private static async Task CorruptRepairedRawSqlCatalogAsync(NpgsqlConnection connection)
    {
        await using var command = new NpgsqlCommand(@"
            DROP INDEX ix_strategyexecution_datapolicy_symbol;
            CREATE INDEX ix_strategyexecution_datapolicy_symbol
              ON ""StrategyExecutions"" ((""DataPolicyJson""->>'market'));

            DROP INDEX uq_data_lake_artifacts_minute_bars;", connection);

        await command.ExecuteNonQueryAsync();
    }

    private static async Task<string[]> GetRepairScopeFingerprintAsync(NpgsqlConnection connection)
    {
        await using var command = new NpgsqlCommand(@"
            SELECT catalog_entry
            FROM (
                SELECT format(
                    'column:%I.%I:%s:%s:%s',
                    table_name,
                    column_name,
                    data_type,
                    is_nullable,
                    coalesce(column_default, '')) AS catalog_entry
                FROM information_schema.columns
                WHERE table_schema = 'public'
                  AND table_name = 'PortfolioSnapshots'
                  AND column_name IN ('NetDelta', 'NetGamma', 'NetTheta', 'NetVega')

                UNION ALL

                SELECT format(
                    'constraint:%s:%I:%s',
                    conrelid::regclass,
                    conname,
                    regexp_replace(pg_get_constraintdef(oid, true), '\s+', ' ', 'g'))
                FROM pg_constraint
                WHERE conname = ANY (@constraintNames)

                UNION ALL

                SELECT format(
                    'index:%s',
                    regexp_replace(pg_get_indexdef(indexrelid), '\s+', ' ', 'g'))
                FROM pg_index
                WHERE indexrelid::regclass::text = ANY (@indexNames)
            ) AS repair_catalog
            ORDER BY catalog_entry;", connection);
        command.Parameters.AddWithValue("constraintNames", RawSqlMigrationConstraints);
        command.Parameters.AddWithValue("indexNames", RawSqlMigrationIndexes);

        await using var reader = await command.ExecuteReaderAsync();
        var fingerprint = new List<string>();
        while (await reader.ReadAsync())
        {
            fingerprint.Add(reader.GetString(0));
        }

        return [.. fingerprint];
    }

    private static async Task<bool> IsMigrationAppliedAsync(NpgsqlConnection connection, string migrationId)
    {
        await using var command = new NpgsqlCommand(
            "SELECT EXISTS (SELECT 1 FROM \"__EFMigrationsHistory\" WHERE \"MigrationId\" = @migrationId);",
            connection);
        command.Parameters.AddWithValue("migrationId", migrationId);

        return (bool)(await command.ExecuteScalarAsync())!;
    }

    private static async Task AssertRelationExistsAsync(NpgsqlConnection connection, string relationName)
    {
        await using var command = new NpgsqlCommand("SELECT to_regclass(@relationName)::text;", connection);
        command.Parameters.AddWithValue("relationName", relationName);

        Assert.IsType<string>(await command.ExecuteScalarAsync());
    }

    private static async Task AssertRelationDoesNotExistAsync(NpgsqlConnection connection, string relationName)
    {
        await using var command = new NpgsqlCommand("SELECT to_regclass(@relationName)::text;", connection);
        command.Parameters.AddWithValue("relationName", relationName);

        Assert.True(await command.ExecuteScalarAsync() is null or DBNull);
    }

    private static string FindRepositoryFile(params string[] relativePathSegments)
    {
        foreach (var startingDirectory in new[] { Directory.GetCurrentDirectory(), AppContext.BaseDirectory })
        {
            for (var directory = new DirectoryInfo(startingDirectory); directory is not null; directory = directory.Parent)
            {
                var candidate = Path.Combine([directory.FullName, .. relativePathSegments]);
                if (File.Exists(candidate))
                {
                    return candidate;
                }
            }
        }

        throw new FileNotFoundException($"Could not locate {Path.Combine(relativePathSegments)} from the test runtime directories.");
    }
}

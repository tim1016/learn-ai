using Backend.Data;
using Backend.Models.MarketData;
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
    [Trait("Category", "PostgresIntegration")]
    public async Task DatabaseInitializer_FreshDatabase_AppliesEveryMigrationAndLeavesNonePending()
    {
        var baseConnectionString = Environment.GetEnvironmentVariable("BACKEND_TEST_POSTGRES_CONNECTION_STRING");
        if (string.IsNullOrWhiteSpace(baseConnectionString))
        {
            return;
        }

        await using var database = await TemporaryPostgresDatabase.CreateAsync(baseConnectionString);
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

        foreach (var tableName in RawSqlMigrationTables)
        {
            await AssertRelationExistsAsync(connection, $"public.{tableName}");
        }

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
    }

    private static readonly string[] RawSqlMigrationTables =
    [
        "bot_lifecycle_events",
        "account_lifecycle_events",
        "operator_gate_snapshots",
        "lifecycle_node_receipts",
        "account_owner_status_snapshots"
    ];

    private static readonly string[] RawSqlMigrationConstraints =
    [
        "ck_bot_lifecycle_events_source_rank_nonnegative",
        "ck_account_lifecycle_events_source_rank_nonnegative",
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
        "ix_bot_lifecycle_events_timeline",
        "ix_account_lifecycle_events_timeline",
        "ix_strategyexecution_datapolicy_symbol"
    ];

    private static async Task AssertRelationExistsAsync(NpgsqlConnection connection, string relationName)
    {
        await using var command = new NpgsqlCommand("SELECT to_regclass(@relationName)::text;", connection);
        command.Parameters.AddWithValue("relationName", relationName);

        Assert.NotNull(await command.ExecuteScalarAsync());
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

    private sealed class TemporaryPostgresDatabase : IAsyncDisposable
    {
        private readonly string _adminConnectionString;
        private readonly string _databaseName;

        private TemporaryPostgresDatabase(string adminConnectionString, string connectionString, string databaseName)
        {
            _adminConnectionString = adminConnectionString;
            ConnectionString = connectionString;
            _databaseName = databaseName;
        }

        public string ConnectionString { get; }

        public static async Task<TemporaryPostgresDatabase> CreateAsync(string baseConnectionString)
        {
            var connectionStringBuilder = new NpgsqlConnectionStringBuilder(baseConnectionString);
            var databaseName = $"backend_migrations_{Guid.NewGuid():N}";
            connectionStringBuilder.Database = "postgres";
            var adminConnectionString = connectionStringBuilder.ConnectionString;

            await using (var connection = new NpgsqlConnection(adminConnectionString))
            {
                await connection.OpenAsync();
                await using var command = new NpgsqlCommand($"CREATE DATABASE \"{databaseName}\";", connection);
                await command.ExecuteNonQueryAsync();
            }

            connectionStringBuilder.Database = databaseName;
            return new TemporaryPostgresDatabase(adminConnectionString, connectionStringBuilder.ConnectionString, databaseName);
        }

        public async ValueTask DisposeAsync()
        {
            await using var connection = new NpgsqlConnection(_adminConnectionString);
            await connection.OpenAsync();

            await using (var terminateConnections = new NpgsqlCommand(
                             "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = @databaseName AND pid <> pg_backend_pid();",
                             connection))
            {
                terminateConnections.Parameters.AddWithValue("databaseName", _databaseName);
                await terminateConnections.ExecuteNonQueryAsync();
            }

            await using var dropDatabase = new NpgsqlCommand($"DROP DATABASE IF EXISTS \"{_databaseName}\";", connection);
            await dropDatabase.ExecuteNonQueryAsync();
        }
    }
}

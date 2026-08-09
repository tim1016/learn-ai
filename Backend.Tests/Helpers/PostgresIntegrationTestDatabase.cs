using Backend.Data;
using Microsoft.EntityFrameworkCore;
using Microsoft.Extensions.DependencyInjection;
using Microsoft.Extensions.Logging.Abstractions;
using Npgsql;
using Xunit.Sdk;

namespace Backend.Tests.Helpers;

/// <summary>
/// Owns an isolated PostgreSQL database for integration tests. Call
/// <see cref="CreateMigratedAsync"/> when the test needs the current schema.
/// The database is dropped on disposal after terminating any leaked sessions.
/// </summary>
public sealed class PostgresIntegrationTestDatabase : IAsyncDisposable
{
    private const string ConnectionStringEnvironmentVariable = "BACKEND_TEST_POSTGRES_CONNECTION_STRING";

    private readonly string _adminConnectionString;
    private readonly string _databaseName;

    private PostgresIntegrationTestDatabase(
        string adminConnectionString,
        string connectionString,
        string databaseName)
    {
        _adminConnectionString = adminConnectionString;
        ConnectionString = connectionString;
        _databaseName = databaseName;
    }

    public string ConnectionString { get; }

    public static async Task<PostgresIntegrationTestDatabase> CreateAsync(
        CancellationToken ct = default)
    {
        var baseConnectionString = Environment.GetEnvironmentVariable(ConnectionStringEnvironmentVariable);
        if (string.IsNullOrWhiteSpace(baseConnectionString))
        {
            throw SkipException.ForSkip(
                $"PostgreSQL integration tests require {ConnectionStringEnvironmentVariable}.");
        }

        var connectionStringBuilder = new NpgsqlConnectionStringBuilder(baseConnectionString);
        var databaseName = $"backend_integration_{Guid.NewGuid():N}";
        connectionStringBuilder.Database = "postgres";
        var adminConnectionString = connectionStringBuilder.ConnectionString;

        await using (var connection = new NpgsqlConnection(adminConnectionString))
        {
            await connection.OpenAsync(ct);
            await using var command = new NpgsqlCommand($"CREATE DATABASE \"{databaseName}\";", connection);
            await command.ExecuteNonQueryAsync(ct);
        }

        connectionStringBuilder.Database = databaseName;
        return new PostgresIntegrationTestDatabase(
            adminConnectionString,
            connectionStringBuilder.ConnectionString,
            databaseName);
    }

    public static async Task<PostgresIntegrationTestDatabase> CreateMigratedAsync(
        CancellationToken ct = default)
    {
        var database = await CreateAsync(ct);
        try
        {
            using var services = new ServiceCollection()
                .AddDbContext<AppDbContext>(options => options.UseNpgsql(database.ConnectionString))
                .BuildServiceProvider();
            await DatabaseInitializer.MigrateAsync(services, NullLogger.Instance, ct);
            return database;
        }
        catch
        {
            await database.DisposeAsync();
            throw;
        }
    }

    public AppDbContext CreateContext(string applicationName)
    {
        var connectionStringBuilder = new NpgsqlConnectionStringBuilder(ConnectionString)
        {
            ApplicationName = applicationName,
        };
        var options = new DbContextOptionsBuilder<AppDbContext>()
            .UseNpgsql(connectionStringBuilder.ConnectionString)
            .Options;
        return new AppDbContext(options);
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

        await using var dropDatabase = new NpgsqlCommand(
            $"DROP DATABASE IF EXISTS \"{_databaseName}\";",
            connection);
        await dropDatabase.ExecuteNonQueryAsync();
    }
}

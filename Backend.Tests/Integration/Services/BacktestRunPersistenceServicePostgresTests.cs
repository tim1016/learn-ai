using System.Diagnostics;
using Backend.Data;
using Backend.Models.MarketData;
using Backend.Services.Implementation;
using Backend.Services.Interfaces;
using Backend.Tests.Helpers;
using Microsoft.EntityFrameworkCore;
using Microsoft.Extensions.Logging.Abstractions;
using Moq;
using Npgsql;

namespace Backend.Tests.Integration.Services;

public class BacktestRunPersistenceServicePostgresTests
{
    private const int LeanInsertBarrierLockKey = 1_426_001;
    private const int BothInsertBarrierLockKey = 1_426_002;

    [Fact]
    [Trait("Category", "PostgresIntegration")]
    public async Task PersistAsync_ConcurrentSameModeRetries_ReturnOnePostgresRow()
    {
        await using var database = await PostgresIntegrationTestDatabase.CreateMigratedAsync();
        await SeedTickerAndInstallInsertBarrierAsync(database.ConnectionString);
        await using var barrier = await InsertBarrier.HoldAsync(
            database.ConnectionString,
            LeanInsertBarrierLockKey);
        await using var firstContext = database.CreateContext("idempotency-same-first");
        await using var secondContext = database.CreateContext("idempotency-same-second");
        var payload = BuildPayload("postgres-race-same", "lean");

        var first = CreateService(firstContext).PersistAsync(payload, CancellationToken.None);
        await WaitForBlockedInsertsAsync(database.ConnectionString, "idempotency-same-", expectedCount: 1);
        var second = CreateService(secondContext).PersistAsync(payload, CancellationToken.None);
        await WaitForBlockedInsertsAsync(database.ConnectionString, "idempotency-same-", expectedCount: 2);

        await barrier.CommitAsync();
        var ids = await Task.WhenAll(first, second);

        await using var verificationContext = database.CreateContext("idempotency-same-verification");
        var rows = await verificationContext.StrategyExecutions
            .AsNoTracking()
            .Where(run => run.Source == "lean-sidecar" && run.LeanRunId == payload.LeanRunId)
            .Select(run => new { run.Id, run.RequestedEngine })
            .ToListAsync();
        Assert.Single(rows);
        Assert.All(ids, id => Assert.Equal(rows[0].Id, id));
        Assert.Equal("lean", rows[0].RequestedEngine);
    }

    [Fact]
    [Trait("Category", "PostgresIntegration")]
    public async Task PersistAsync_ConcurrentConflictingModes_RejectsLosingRetry()
    {
        await using var database = await PostgresIntegrationTestDatabase.CreateMigratedAsync();
        await SeedTickerAndInstallInsertBarrierAsync(database.ConnectionString);
        await using var winnerBarrier = await InsertBarrier.HoldAsync(
            database.ConnectionString,
            LeanInsertBarrierLockKey);
        await using var loserBarrier = await InsertBarrier.HoldAsync(
            database.ConnectionString,
            BothInsertBarrierLockKey);
        await using var firstContext = database.CreateContext("idempotency-conflict-first");
        await using var secondContext = database.CreateContext("idempotency-conflict-second");
        var leanPayload = BuildPayload("postgres-race-conflict", "lean");
        var bothPayload = leanPayload with { RequestedEngine = "both" };

        var winner = CreateService(firstContext).PersistAsync(leanPayload, CancellationToken.None);
        await WaitForBlockedInsertsAsync(database.ConnectionString, "idempotency-conflict-", expectedCount: 1);
        var loser = CreateService(secondContext).PersistAsync(bothPayload, CancellationToken.None);
        await WaitForBlockedInsertsAsync(database.ConnectionString, "idempotency-conflict-", expectedCount: 2);

        await winnerBarrier.CommitAsync();
        var winnerId = await winner;
        await loserBarrier.CommitAsync();
        var error = await Assert.ThrowsAsync<ArgumentException>(async () =>
        {
            await loser;
        });

        Assert.Contains("requested_engine", error.Message, StringComparison.Ordinal);
        await using var verificationContext = database.CreateContext("idempotency-conflict-verification");
        var rows = await verificationContext.StrategyExecutions
            .AsNoTracking()
            .Where(run => run.Source == "lean-sidecar" && run.LeanRunId == leanPayload.LeanRunId)
            .Select(run => new { run.Id, run.RequestedEngine })
            .ToListAsync();
        Assert.Single(rows);
        Assert.Equal(winnerId, rows[0].Id);
        Assert.Equal("lean", rows[0].RequestedEngine);
    }

    private static BacktestRunPersistenceService CreateService(AppDbContext context) =>
        new(
            context,
            Mock.Of<IParityVerdictService>(),
            NullLogger<BacktestRunPersistenceService>.Instance);

    private static PersistLeanRunPayload BuildPayload(string leanRunId, string requestedEngine) =>
        new(
            LeanRunId: leanRunId,
            Source: "lean-sidecar",
            StrategyName: "ema_crossover",
            Symbol: "SPY",
            StartingCash: 100_000m,
            StartDateMs: 1_700_000_000_000,
            EndDateMs: 1_700_000_600_000,
            TotalTrades: 0,
            WinningTrades: 0,
            LosingTrades: 0,
            TotalPnl: 0m,
            TotalFees: 0m,
            FinalEquity: 100_000m,
            WinRate: 0,
            Trades: [],
            LeanStatistics: null)
        {
            RequestedEngine = requestedEngine,
        };

    private static async Task SeedTickerAndInstallInsertBarrierAsync(string connectionString)
    {
        await using var connection = new NpgsqlConnection(connectionString);
        await connection.OpenAsync();
        await using var command = new NpgsqlCommand(
            $$"""
              INSERT INTO "Tickers" ("Symbol", "Name", "Market", "Active", "CreatedAt")
              VALUES ('SPY', 'SPY', 'stocks', TRUE, now());

              CREATE FUNCTION block_strategy_execution_insert_for_idempotency_test()
              RETURNS trigger
              LANGUAGE plpgsql
              AS $function$
              BEGIN
                  PERFORM pg_advisory_xact_lock(
                      CASE NEW."RequestedEngine"
                          WHEN 'both' THEN {{BothInsertBarrierLockKey}}
                          ELSE {{LeanInsertBarrierLockKey}}
                      END);
                  RETURN NEW;
              END;
              $function$;

              CREATE TRIGGER block_strategy_execution_insert_for_idempotency_test
              BEFORE INSERT ON "StrategyExecutions"
              FOR EACH ROW
              EXECUTE FUNCTION block_strategy_execution_insert_for_idempotency_test();
              """,
            connection);
        await command.ExecuteNonQueryAsync();
    }

    private static async Task WaitForBlockedInsertsAsync(
        string connectionString,
        string applicationNamePrefix,
        int expectedCount)
    {
        var timeout = TimeSpan.FromSeconds(15);
        var stopwatch = Stopwatch.StartNew();
        await using var connection = new NpgsqlConnection(connectionString);
        await connection.OpenAsync();

        while (stopwatch.Elapsed < timeout)
        {
            await using var command = new NpgsqlCommand(
                """
                SELECT count(*)
                FROM pg_stat_activity
                WHERE datname = current_database()
                  AND application_name LIKE @applicationNamePattern
                  AND wait_event_type = 'Lock'
                  AND wait_event = 'advisory';
                """,
                connection);
            command.Parameters.AddWithValue("applicationNamePattern", $"{applicationNamePrefix}%");
            var blockedCount = Convert.ToInt32(await command.ExecuteScalarAsync());
            if (blockedCount == expectedCount)
            {
                return;
            }

            await Task.Delay(TimeSpan.FromMilliseconds(20));
        }

        throw new TimeoutException(
            $"Expected {expectedCount} inserts with application-name prefix " +
            $"'{applicationNamePrefix}' to block on the PostgreSQL advisory lock within {timeout}.");
    }

    private sealed class InsertBarrier : IAsyncDisposable
    {
        private readonly NpgsqlConnection _connection;
        private readonly NpgsqlTransaction _transaction;

        private InsertBarrier(NpgsqlConnection connection, NpgsqlTransaction transaction)
        {
            _connection = connection;
            _transaction = transaction;
        }

        public static async Task<InsertBarrier> HoldAsync(string connectionString, int lockKey)
        {
            var connection = new NpgsqlConnection(connectionString);
            await connection.OpenAsync();
            try
            {
                var transaction = await connection.BeginTransactionAsync();
                await using var command = new NpgsqlCommand(
                    "SELECT pg_advisory_xact_lock(@lockKey);",
                    connection,
                    transaction);
                command.Parameters.AddWithValue("lockKey", lockKey);
                await command.ExecuteNonQueryAsync();
                return new InsertBarrier(connection, transaction);
            }
            catch
            {
                await connection.DisposeAsync();
                throw;
            }
        }

        public async Task CommitAsync() => await _transaction.CommitAsync();

        public async ValueTask DisposeAsync()
        {
            await _transaction.DisposeAsync();
            await _connection.DisposeAsync();
        }
    }
}

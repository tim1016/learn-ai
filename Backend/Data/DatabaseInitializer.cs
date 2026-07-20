using Microsoft.EntityFrameworkCore;
using Microsoft.Extensions.DependencyInjection;
using Microsoft.Extensions.Logging;
using Npgsql;

namespace Backend.Data;

/// <summary>
/// Applies the application's EF Core migration chain during controlled startup.
/// </summary>
public static class DatabaseInitializer
{
    private const int MaxRetries = 10;
    private static readonly TimeSpan RetryDelay = TimeSpan.FromSeconds(2);

    public static async Task MigrateAsync(
        IServiceProvider services,
        ILogger logger,
        CancellationToken cancellationToken)
    {
        await ExecuteWithRetryAsync(
            async cancellationToken =>
            {
                await using var scope = services.CreateAsyncScope();
                var context = scope.ServiceProvider.GetRequiredService<AppDbContext>();

                await context.Database.MigrateAsync(cancellationToken);
            },
            logger,
            cancellationToken,
            RetryDelay);

        logger.LogInformation("Database migrations applied successfully.");
    }

    internal static async Task ExecuteWithRetryAsync(
        Func<CancellationToken, Task> operation,
        ILogger logger,
        CancellationToken cancellationToken,
        TimeSpan retryDelay)
    {
        for (var attempt = 1; attempt <= MaxRetries; attempt++)
        {
            try
            {
                await operation(cancellationToken);
                return;
            }
            catch (OperationCanceledException) when (cancellationToken.IsCancellationRequested)
            {
                throw;
            }
            catch (Exception exception) when (IsTransientDatabaseFailure(exception) && attempt < MaxRetries)
            {
                logger.LogWarning(
                    exception,
                    "Database migration attempt {Attempt}/{MaxRetries} failed; retrying in {RetryDelaySeconds} seconds.",
                    attempt,
                    MaxRetries,
                    retryDelay.TotalSeconds);

                await Task.Delay(retryDelay, cancellationToken);
            }
            catch (Exception exception)
            {
                logger.LogError(
                    exception,
                    "Database migrations failed on attempt {Attempt}/{MaxRetries}; the error is not retryable.",
                    attempt,
                    MaxRetries);
                throw;
            }
        }
    }

    private static bool IsTransientDatabaseFailure(Exception exception) =>
        exception is TimeoutException or NpgsqlException { IsTransient: true };
}

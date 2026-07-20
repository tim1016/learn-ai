using Microsoft.EntityFrameworkCore;
using Microsoft.Extensions.DependencyInjection;
using Microsoft.Extensions.Logging;

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
        for (var attempt = 1; attempt <= MaxRetries; attempt++)
        {
            try
            {
                await using var scope = services.CreateAsyncScope();
                var context = scope.ServiceProvider.GetRequiredService<AppDbContext>();

                await context.Database.MigrateAsync(cancellationToken);
                logger.LogInformation("Database migrations applied successfully.");
                return;
            }
            catch (OperationCanceledException) when (cancellationToken.IsCancellationRequested)
            {
                throw;
            }
            catch (Exception exception)
            {
                if (attempt == MaxRetries)
                {
                    logger.LogError(
                        exception,
                        "Database migrations failed after {MaxRetries} attempts.",
                        MaxRetries);
                    throw;
                }

                logger.LogWarning(
                    exception,
                    "Database migration attempt {Attempt}/{MaxRetries} failed; retrying in {RetryDelaySeconds} seconds.",
                    attempt,
                    MaxRetries,
                    RetryDelay.TotalSeconds);

                await Task.Delay(RetryDelay, cancellationToken);
            }
        }
    }
}

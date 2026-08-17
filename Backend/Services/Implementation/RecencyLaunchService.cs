using Backend.Data;
using Backend.Models.MarketData;
using Backend.Services.Interfaces;
using Microsoft.EntityFrameworkCore;

namespace Backend.Services.Implementation;

public class RecencyLaunchService : IRecencyLaunchService
{
    private readonly AppDbContext _db;

    public RecencyLaunchService(AppDbContext db)
    {
        _db = db;
    }

    public async Task CreateLaunchAsync(string launchId, string configJson, int expectedRuns, CancellationToken ct)
    {
        if (expectedRuns <= 0)
            throw new ArgumentOutOfRangeException(nameof(expectedRuns), "Expected runs must be positive.");

        var exists = await _db.RecencyLaunches.AsNoTracking().AnyAsync(l => l.Id == launchId, ct);
        if (exists)
            return; // idempotent — a retried dispatch must not duplicate or reset the row

        _db.RecencyLaunches.Add(new RecencyLaunch
        {
            Id = launchId,
            ConfigJson = configJson,
            ExpectedRuns = expectedRuns,
            Status = "RUNNING",
            CreatedAtMs = DateTimeOffset.UtcNow.ToUnixTimeMilliseconds(),
        });
        await _db.SaveChangesAsync(ct);
    }

    public async Task<bool> SetTerminalStatusAsync(
        string launchId,
        string status,
        int? succeededRuns,
        int? failedRuns,
        CancellationToken ct)
    {
        if (status is not ("COMPLETED" or "CANCELLED" or "FAILED"))
            throw new ArgumentException("Status must be COMPLETED, CANCELLED, or FAILED.", nameof(status));
        if (succeededRuns is < 0 || failedRuns is < 0)
            throw new ArgumentOutOfRangeException(nameof(succeededRuns), "Run counts cannot be negative.");

        var launch = await _db.RecencyLaunches.FirstOrDefaultAsync(l => l.Id == launchId, ct);
        if (launch is null)
            return false;

        if (succeededRuns is not null)
            launch.SucceededRuns = succeededRuns.Value;
        if (failedRuns is not null)
            launch.FailedRuns = failedRuns.Value;
        if (launch.SucceededRuns + launch.FailedRuns > launch.ExpectedRuns)
            throw new ArgumentException("Succeeded plus failed runs cannot exceed expected runs.");
        if (status is "COMPLETED" or "FAILED" && launch.SucceededRuns + launch.FailedRuns != launch.ExpectedRuns)
            throw new ArgumentException("Completed or failed launches must account for every expected run.");

        launch.Status = status;
        launch.CompletedAtMs = DateTimeOffset.UtcNow.ToUnixTimeMilliseconds();
        await _db.SaveChangesAsync(ct);
        return true;
    }
}

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

    public async Task CreateLaunchAsync(string launchId, string configJson, CancellationToken ct)
    {
        var exists = await _db.RecencyLaunches.AsNoTracking().AnyAsync(l => l.Id == launchId, ct);
        if (exists)
            return; // idempotent — a retried dispatch must not duplicate or reset the row

        _db.RecencyLaunches.Add(new RecencyLaunch
        {
            Id = launchId,
            ConfigJson = configJson,
            Status = "RUNNING",
            CreatedAtMs = DateTimeOffset.UtcNow.ToUnixTimeMilliseconds(),
        });
        await _db.SaveChangesAsync(ct);
    }
}

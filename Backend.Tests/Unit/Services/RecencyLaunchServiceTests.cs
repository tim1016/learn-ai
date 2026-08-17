using Backend.Data;
using Backend.Services.Implementation;
using Backend.Tests.Helpers;
using Microsoft.EntityFrameworkCore;

namespace Backend.Tests.Unit.Services;

public class RecencyLaunchServiceTests
{
    private static RecencyLaunchService CreateService(out AppDbContext db)
    {
        db = TestDbContextFactory.Create();
        return new RecencyLaunchService(db);
    }

    [Fact]
    public async Task CreateLaunchAsync_NewId_InsertsRunningLaunchWithConfig()
    {
        var service = CreateService(out var db);

        await service.CreateLaunchAsync("launch-1", "{\"symbols\":[\"SPY\"]}", CancellationToken.None);

        var launch = await db.RecencyLaunches.SingleAsync(l => l.Id == "launch-1");
        Assert.Equal("RUNNING", launch.Status);
        Assert.Equal("{\"symbols\":[\"SPY\"]}", launch.ConfigJson);
        Assert.Equal(0, launch.SucceededRuns);
        Assert.Equal(0, launch.FailedRuns);
        Assert.Null(launch.DeletedAtMs);
    }

    [Fact]
    public async Task CreateLaunchAsync_CalledTwiceWithSameId_DoesNotDuplicateTheRow()
    {
        var service = CreateService(out var db);

        await service.CreateLaunchAsync("launch-1", "{}", CancellationToken.None);
        await service.CreateLaunchAsync("launch-1", "{}", CancellationToken.None);

        Assert.Equal(1, await db.RecencyLaunches.CountAsync(l => l.Id == "launch-1"));
    }
}

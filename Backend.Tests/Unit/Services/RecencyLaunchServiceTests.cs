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

        await service.CreateLaunchAsync("launch-1", "{\"symbols\":[\"SPY\"]}", 4, CancellationToken.None);

        var launch = await db.RecencyLaunches.SingleAsync(l => l.Id == "launch-1");
        Assert.Equal("RUNNING", launch.Status);
        Assert.Equal("{\"symbols\":[\"SPY\"]}", launch.ConfigJson);
        Assert.Equal(4, launch.ExpectedRuns);
        Assert.Equal(0, launch.SucceededRuns);
        Assert.Equal(0, launch.FailedRuns);
        Assert.Null(launch.DeletedAtMs);
    }

    [Fact]
    public async Task CreateLaunchAsync_CalledTwiceWithSameId_DoesNotDuplicateTheRow()
    {
        var service = CreateService(out var db);

        await service.CreateLaunchAsync("launch-1", "{}", 1, CancellationToken.None);
        await service.CreateLaunchAsync("launch-1", "{}", 1, CancellationToken.None);

        Assert.Equal(1, await db.RecencyLaunches.CountAsync(l => l.Id == "launch-1"));
    }

    [Fact]
    public async Task SetTerminalStatusAsync_PersistsCountsAndCompletionTime()
    {
        var service = CreateService(out var db);
        await service.CreateLaunchAsync("launch-1", "{}", 4, CancellationToken.None);

        var found = await service.SetTerminalStatusAsync("launch-1", "FAILED", 3, 1, CancellationToken.None);

        Assert.True(found);
        var launch = await db.RecencyLaunches.SingleAsync(l => l.Id == "launch-1");
        Assert.Equal("FAILED", launch.Status);
        Assert.Equal(3, launch.SucceededRuns);
        Assert.Equal(1, launch.FailedRuns);
        Assert.NotNull(launch.CompletedAtMs);
    }

    [Fact]
    public async Task SetTerminalStatusAsync_RejectsUnaccountedTerminalRuns()
    {
        var service = CreateService(out _);
        await service.CreateLaunchAsync("launch-1", "{}", 4, CancellationToken.None);

        await Assert.ThrowsAsync<ArgumentException>(() =>
            service.SetTerminalStatusAsync("launch-1", "COMPLETED", 2, 0, CancellationToken.None));
    }
}

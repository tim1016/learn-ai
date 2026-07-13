using Backend.Models.Comparison;
using Backend.Models.MarketData;
using Backend.Services.Implementation;
using Backend.Services.Interfaces;
using Backend.Tests.Helpers;
using Microsoft.EntityFrameworkCore;
using Microsoft.Extensions.Logging.Abstractions;
using Moq;

namespace Backend.Tests.Unit.Services;

public class ParityVerdictServiceTests
{
    private static ParityVerdictService CreateService(
        Backend.Data.AppDbContext db,
        CompareTradesResponse comparison)
    {
        var comparisonService = new Mock<IComparisonService>();
        comparisonService
            .Setup(c => c.CompareTradesAsync(It.IsAny<CompareTradesRequest>(), It.IsAny<CancellationToken>()))
            .ReturnsAsync(comparison);
        return new ParityVerdictService(db, comparisonService.Object, NullLogger<ParityVerdictService>.Instance);
    }

    private static async Task<(int leftId, int rightId)> SeedGroupAsync(
        Backend.Data.AppDbContext db,
        string group,
        string? pendingStatus = "pending")
    {
        var ticker = new Ticker { Symbol = "SPY", Name = "SPY", Market = "stocks" };
        var left = new StrategyExecution
        {
            Ticker = ticker,
            Source = "engine",
            StrategyName = "spy_ema_crossover",
            ParityGroupId = group,
        };
        var right = new StrategyExecution
        {
            Ticker = ticker,
            Source = "lean-sidecar",
            StrategyName = "ema_crossover",
            LeanRunId = $"companion-{group}",
            ParityGroupId = group,
        };
        db.StrategyExecutions.AddRange(left, right);
        await db.SaveChangesAsync();

        if (pendingStatus is not null)
        {
            db.ParityVerdicts.Add(new ParityVerdict
            {
                LeftExecutionId = left.Id,
                RightExecutionId = null,
                ParityGroupId = group,
                VerdictVersion = ParityVerdictService.VerdictVersion,
                Status = pendingStatus,
                VerdictJson = "{}",
            });
            await db.SaveChangesAsync();
        }

        return (left.Id, right.Id);
    }

    private static CompareTradesResponse NoDivergences() => new([], null);

    private static CompareTradesResponse WithDivergence() => new(
        [new TradeDivergenceRecord("FILL_PRICE_DRIFT", 1, 1_700_000_000_000, "fill differs")],
        1_700_000_000_000);

    [Fact]
    public async Task Compute_NoDivergences_FreezesAgreeOntoPendingRow()
    {
        var db = TestDbContextFactory.Create();
        var (leftId, rightId) = await SeedGroupAsync(db, "pg-agree");
        var service = CreateService(db, NoDivergences());

        await service.ComputeForLeanRunAsync(rightId, "pg-agree", CancellationToken.None);

        var row = await db.ParityVerdicts.SingleAsync(v => v.ParityGroupId == "pg-agree");
        Assert.Equal("agree", row.Status);
        Assert.Equal(rightId, row.RightExecutionId);
        Assert.Equal(leftId, row.LeftExecutionId);
        Assert.Contains("\"status\":\"agree\"", row.VerdictJson);
    }

    [Fact]
    public async Task Compute_WithDivergences_FreezesDivergedWithCategoryCounts()
    {
        var db = TestDbContextFactory.Create();
        var (_, rightId) = await SeedGroupAsync(db, "pg-div");
        var service = CreateService(db, WithDivergence());

        await service.ComputeForLeanRunAsync(rightId, "pg-div", CancellationToken.None);

        var row = await db.ParityVerdicts.SingleAsync(v => v.ParityGroupId == "pg-div");
        Assert.Equal("diverged", row.Status);
        Assert.Contains("FILL_PRICE_DRIFT", row.VerdictJson);
        Assert.Contains("counts_by_category", row.VerdictJson);
    }

    [Fact]
    public async Task Compute_TerminalRow_IsNeverOverwritten()
    {
        var db = TestDbContextFactory.Create();
        var (_, rightId) = await SeedGroupAsync(db, "pg-final", pendingStatus: "run_failed");
        var service = CreateService(db, NoDivergences());

        await service.ComputeForLeanRunAsync(rightId, "pg-final", CancellationToken.None);

        var row = await db.ParityVerdicts.SingleAsync(v => v.ParityGroupId == "pg-final");
        Assert.Equal("run_failed", row.Status);
    }

    [Fact]
    public async Task Compute_MissingPendingRow_InsertsTerminalVerdict()
    {
        var db = TestDbContextFactory.Create();
        var (_, rightId) = await SeedGroupAsync(db, "pg-lost", pendingStatus: null);
        var service = CreateService(db, NoDivergences());

        await service.ComputeForLeanRunAsync(rightId, "pg-lost", CancellationToken.None);

        var row = await db.ParityVerdicts.SingleAsync(v => v.ParityGroupId == "pg-lost");
        Assert.Equal("agree", row.Status);
    }

    [Fact]
    public async Task Compute_MissingLeftRun_LeavesVerdictPending()
    {
        var db = TestDbContextFactory.Create();
        var ticker = new Ticker { Symbol = "SPY", Name = "SPY", Market = "stocks" };
        var right = new StrategyExecution
        {
            Ticker = ticker,
            Source = "lean-sidecar",
            StrategyName = "ema_crossover",
            LeanRunId = "companion-pg-orphan",
            ParityGroupId = "pg-orphan",
        };
        db.StrategyExecutions.Add(right);
        await db.SaveChangesAsync();
        var service = CreateService(db, NoDivergences());

        await service.ComputeForLeanRunAsync(right.Id, "pg-orphan", CancellationToken.None);

        Assert.False(await db.ParityVerdicts.AnyAsync(v => v.ParityGroupId == "pg-orphan"));
    }
}

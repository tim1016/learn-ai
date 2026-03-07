using Backend.Models.MarketData;
using Backend.Models.Portfolio;
using Backend.Services.Implementation;
using Backend.Services.Interfaces;
using Backend.Tests.Helpers;
using Microsoft.Extensions.Logging;
using Moq;

namespace Backend.Tests.Unit.Services;

public class SnapshotServiceTests
{
    private static (SnapshotService service, Backend.Data.AppDbContext context, Mock<IPortfolioValuationService> valuationMock) CreateServiceWithContext()
    {
        var context = TestDbContextFactory.Create();
        var valuationMock = new Mock<IPortfolioValuationService>();
        var logger = new Mock<ILogger<SnapshotService>>();
        var service = new SnapshotService(context, valuationMock.Object, logger.Object);
        return (service, context, valuationMock);
    }

    private static Account SeedAccount(Backend.Data.AppDbContext context, decimal cash = 100_000m)
    {
        var account = new Account
        {
            Id = Guid.NewGuid(),
            Name = "Test",
            Type = AccountType.Paper,
            InitialCash = cash,
            Cash = cash,
        };
        context.Accounts.Add(account);
        context.SaveChanges();
        return account;
    }

    #region TakeSnapshot

    [Fact]
    public async Task TakeSnapshot_PersistsAllFields()
    {
        var (service, context, valuationMock) = CreateServiceWithContext();
        var account = SeedAccount(context);

        valuationMock.Setup(v => v.ComputeValuationAsync(account.Id, It.IsAny<CancellationToken>()))
            .ReturnsAsync(new PortfolioValuation
            {
                Cash = 80_000m,
                MarketValue = 20_000m,
                Equity = 100_000m,
                UnrealizedPnL = 5_000m,
                RealizedPnL = 2_000m,
                NetDelta = 150m,
                NetGamma = 5m,
                NetTheta = -25m,
                NetVega = 100m,
            });

        var snapshot = await service.TakeSnapshotAsync(account.Id);

        Assert.NotEqual(Guid.Empty, snapshot.Id);
        Assert.Equal(account.Id, snapshot.AccountId);
        Assert.Equal(80_000m, snapshot.Cash);
        Assert.Equal(20_000m, snapshot.MarketValue);
        Assert.Equal(100_000m, snapshot.Equity);
        Assert.Equal(5_000m, snapshot.UnrealizedPnL);
        Assert.Equal(2_000m, snapshot.RealizedPnL);
        Assert.Equal(150m, snapshot.NetDelta);
        Assert.Equal(5m, snapshot.NetGamma);
        Assert.Equal(-25m, snapshot.NetTheta);
        Assert.Equal(100m, snapshot.NetVega);

        // Verify persisted to DB
        var saved = context.PortfolioSnapshots.FirstOrDefault(s => s.Id == snapshot.Id);
        Assert.NotNull(saved);
    }

    #endregion

    #region GetEquityCurve

    [Fact]
    public async Task GetEquityCurve_ReturnsOrderedSeries()
    {
        var (service, context, _) = CreateServiceWithContext();
        var account = SeedAccount(context);
        var baseTime = new DateTime(2026, 1, 1, 10, 0, 0, DateTimeKind.Utc);

        // Insert snapshots out of order
        for (int i = 9; i >= 0; i--)
        {
            context.PortfolioSnapshots.Add(new PortfolioSnapshot
            {
                Id = Guid.NewGuid(),
                AccountId = account.Id,
                Timestamp = baseTime.AddDays(i),
                Equity = 100_000m + i * 1000m,
                Cash = 80_000m,
                MarketValue = 20_000m + i * 1000m,
            });
        }
        await context.SaveChangesAsync();

        var curve = await service.GetEquityCurveAsync(account.Id);

        Assert.Equal(10, curve.Count);
        // Should be sorted by timestamp ascending
        for (int i = 1; i < curve.Count; i++)
        {
            Assert.True(curve[i].Timestamp > curve[i - 1].Timestamp);
        }
    }

    [Fact]
    public async Task GetEquityCurve_WithDateRange_FiltersCorrectly()
    {
        var (service, context, _) = CreateServiceWithContext();
        var account = SeedAccount(context);
        var baseTime = new DateTime(2026, 1, 1, 10, 0, 0, DateTimeKind.Utc);

        for (int i = 0; i < 10; i++)
        {
            context.PortfolioSnapshots.Add(new PortfolioSnapshot
            {
                Id = Guid.NewGuid(),
                AccountId = account.Id,
                Timestamp = baseTime.AddDays(i),
                Equity = 100_000m + i * 1000m,
                Cash = 80_000m,
                MarketValue = 20_000m + i * 1000m,
            });
        }
        await context.SaveChangesAsync();

        var curve = await service.GetEquityCurveAsync(account.Id,
            from: baseTime.AddDays(3), to: baseTime.AddDays(6));

        Assert.Equal(4, curve.Count); // days 3, 4, 5, 6
    }

    #endregion

    #region ComputeMetrics — Sharpe Ratio

    [Fact]
    public void ComputeMetrics_SharpeRatio_KnownSeries()
    {
        var (service, _, _) = CreateServiceWithContext();
        var baseTime = new DateTime(2026, 1, 1, 10, 0, 0, DateTimeKind.Utc);

        // Create an upward equity curve with some variance (not constant returns)
        var equities = new[] { 100_000m, 101_000m, 100_500m, 102_000m, 101_500m,
                               103_000m, 102_500m, 104_000m, 103_500m, 105_000m, 106_000m };
        var snapshots = equities.Select((eq, i) => new PortfolioSnapshot
        {
            Id = Guid.NewGuid(),
            Timestamp = baseTime.AddDays(i),
            Equity = eq,
            Cash = 50_000m,
            MarketValue = eq - 50_000m,
        }).ToList();

        var metrics = service.ComputeMetrics(snapshots);

        // Positive mean return with some variance → positive Sharpe
        Assert.True(metrics.SharpeRatio > 0);
        Assert.True(metrics.TotalReturnPercent > 0);
        Assert.Equal(11, metrics.SnapshotCount);
    }

    #endregion

    #region ComputeMetrics — Max Drawdown

    [Fact]
    public void ComputeMetrics_MaxDrawdown_KnownSeries()
    {
        var (service, _, _) = CreateServiceWithContext();
        var baseTime = new DateTime(2026, 1, 1, 10, 0, 0, DateTimeKind.Utc);

        // Equity: 100, 110, 90, 105
        var equities = new[] { 100m, 110m, 90m, 105m };
        var snapshots = equities.Select((eq, i) => new PortfolioSnapshot
        {
            Id = Guid.NewGuid(),
            Timestamp = baseTime.AddDays(i),
            Equity = eq,
            Cash = eq,
        }).ToList();

        var metrics = service.ComputeMetrics(snapshots);

        // MaxDrawdown = 110 - 90 = 20
        Assert.Equal(20m, metrics.MaxDrawdown);
        // MaxDrawdownPercent = 20/110 = 0.1818...
        Assert.True(Math.Abs(metrics.MaxDrawdownPercent - 0.1818m) < 0.001m);
    }

    #endregion

    #region ComputeMetrics — Edge Cases

    [Fact]
    public void ComputeMetrics_SingleSnapshot_ReturnsEmpty()
    {
        var (service, _, _) = CreateServiceWithContext();

        var snapshots = new List<PortfolioSnapshot>
        {
            new() { Id = Guid.NewGuid(), Timestamp = DateTime.UtcNow, Equity = 100_000m }
        };

        var metrics = service.ComputeMetrics(snapshots);

        Assert.Equal(1, metrics.SnapshotCount);
        Assert.Equal(0m, metrics.SharpeRatio);
    }

    [Fact]
    public void ComputeMetrics_WinRate_CalculatedCorrectly()
    {
        var (service, _, _) = CreateServiceWithContext();
        var baseTime = new DateTime(2026, 1, 1, 10, 0, 0, DateTimeKind.Utc);

        // 3 up days, 2 down days → WinRate = 3/5 = 0.6
        var equities = new[] { 100m, 102m, 101m, 104m, 103m, 106m };
        var snapshots = equities.Select((eq, i) => new PortfolioSnapshot
        {
            Id = Guid.NewGuid(),
            Timestamp = baseTime.AddDays(i),
            Equity = eq,
            Cash = eq,
        }).ToList();

        var metrics = service.ComputeMetrics(snapshots);

        Assert.Equal(0.6m, metrics.WinRate);
    }

    #endregion

    #region GetDrawdownSeries

    [Fact]
    public async Task GetDrawdownSeries_ComputesPeakAndDrawdown()
    {
        var (service, context, _) = CreateServiceWithContext();
        var account = SeedAccount(context);
        var baseTime = new DateTime(2026, 1, 1, 10, 0, 0, DateTimeKind.Utc);

        var equities = new[] { 100m, 110m, 90m, 105m, 115m };
        foreach (var (eq, i) in equities.Select((e, i) => (e, i)))
        {
            context.PortfolioSnapshots.Add(new PortfolioSnapshot
            {
                Id = Guid.NewGuid(),
                AccountId = account.Id,
                Timestamp = baseTime.AddDays(i),
                Equity = eq,
                Cash = eq,
            });
        }
        await context.SaveChangesAsync();

        var series = await service.GetDrawdownSeriesAsync(account.Id);

        Assert.Equal(5, series.Count);
        // At index 2 (equity=90), peak was 110, drawdown = 20
        Assert.Equal(110m, series[2].PeakEquity);
        Assert.Equal(20m, series[2].Drawdown);
        // At index 4 (equity=115), new peak, drawdown = 0
        Assert.Equal(115m, series[4].PeakEquity);
        Assert.Equal(0m, series[4].Drawdown);
    }

    #endregion
}

using Backend.Models.MarketData;
using Backend.Models.Portfolio;
using Backend.Services.Implementation;
using Backend.Services.Interfaces;
using Backend.Tests.Helpers;
using Microsoft.Extensions.Logging;
using Moq;

namespace Backend.Tests.Unit.Services;

public class PortfolioReconciliationServiceTests
{
    private static (PortfolioReconciliationService service, Backend.Data.AppDbContext context) CreateServiceWithContext()
    {
        var context = TestDbContextFactory.Create();
        var positionEngine = new PositionEngine(context, new Mock<ILogger<PositionEngine>>().Object);
        var logger = new Mock<ILogger<PortfolioReconciliationService>>();
        var service = new PortfolioReconciliationService(context, positionEngine, logger.Object);
        return (service, context);
    }

    private static (Account account, Ticker ticker) SeedAccountAndTicker(
        Backend.Data.AppDbContext context, decimal cash = 100_000m)
    {
        var ticker = new Ticker { Symbol = "AAPL", Name = "Apple Inc", Market = "stocks" };
        context.Tickers.Add(ticker);
        context.SaveChanges();

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

        return (account, ticker);
    }

    private static void AddTrade(Backend.Data.AppDbContext context,
        Guid accountId, int tickerId, OrderSide side, decimal quantity, decimal price, DateTime timestamp)
    {
        var order = new Order
        {
            Id = Guid.NewGuid(),
            AccountId = accountId,
            TickerId = tickerId,
            Side = side,
            OrderType = OrderType.Market,
            Quantity = quantity,
            Status = OrderStatus.Filled,
            SubmittedAt = timestamp,
            FilledAt = timestamp,
        };
        context.Orders.Add(order);

        var trade = new PortfolioTrade
        {
            Id = Guid.NewGuid(),
            AccountId = accountId,
            OrderId = order.Id,
            TickerId = tickerId,
            Side = side,
            Quantity = quantity,
            Price = price,
            Fees = 0,
            Multiplier = 1,
            ExecutionTimestamp = timestamp,
        };
        context.PortfolioTrades.Add(trade);
        context.SaveChanges();
    }

    #region Reconcile — No Drift

    [Fact]
    public async Task Reconcile_ConsistentPositions_NoDrift()
    {
        var (service, context) = CreateServiceWithContext();
        var (account, ticker) = SeedAccountAndTicker(context);

        // Add a buy trade
        AddTrade(context, account.Id, ticker.Id, OrderSide.Buy, 100, 150m, DateTime.UtcNow);

        // Build positions from trades first (so cached = rebuilt)
        var positionEngine = new PositionEngine(context, new Mock<ILogger<PositionEngine>>().Object);
        await positionEngine.RebuildPositionsAsync(account.Id);

        var report = await service.ReconcileAsync(account.Id);

        Assert.False(report.HasDrift);
        Assert.Empty(report.Drifts);
        Assert.Equal(report.CachedPositionCount, report.RebuiltPositionCount);
    }

    #endregion

    #region Reconcile — Drift Detected

    [Fact]
    public async Task Reconcile_ManuallyAlteredPosition_DetectsDrift()
    {
        var (service, context) = CreateServiceWithContext();
        var (account, ticker) = SeedAccountAndTicker(context);

        // Add a buy trade
        AddTrade(context, account.Id, ticker.Id, OrderSide.Buy, 100, 150m, DateTime.UtcNow);

        // Build correct positions
        var positionEngine = new PositionEngine(context, new Mock<ILogger<PositionEngine>>().Object);
        await positionEngine.RebuildPositionsAsync(account.Id);

        // Manually tamper with the cached position
        var position = context.Positions.First(p => p.AccountId == account.Id);
        position.NetQuantity = 999; // wrong
        await context.SaveChangesAsync();

        var report = await service.ReconcileAsync(account.Id);

        // After reconciliation, positions were rebuilt (fixing the tamper),
        // but cached snapshot had 999 vs rebuilt 100
        Assert.True(report.HasDrift);
        Assert.NotEmpty(report.Drifts);
        Assert.Contains(report.Drifts, d => d.CachedQuantity == 999 && d.RebuiltQuantity == 100);
    }

    #endregion
}

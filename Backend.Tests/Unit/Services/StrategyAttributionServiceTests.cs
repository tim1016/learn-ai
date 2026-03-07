using Backend.Models.MarketData;
using Backend.Models.Portfolio;
using Backend.Services.Implementation;
using Backend.Services.Interfaces;
using Backend.Tests.Helpers;
using Microsoft.Extensions.Logging;
using Moq;

namespace Backend.Tests.Unit.Services;

public class StrategyAttributionServiceTests
{
    private static (StrategyAttributionService service, Backend.Data.AppDbContext context) CreateServiceWithContext()
    {
        var context = TestDbContextFactory.Create();
        var positionEngine = new PositionEngine(context, new Mock<ILogger<PositionEngine>>().Object);
        var logger = new Mock<ILogger<StrategyAttributionService>>();
        var service = new StrategyAttributionService(context, positionEngine, logger.Object);
        return (service, context);
    }

    private static (Account account, Ticker ticker, StrategyExecution execution) SeedFullSetup(
        Backend.Data.AppDbContext context, int tradeCount = 2)
    {
        var ticker = new Ticker { Symbol = "AAPL", Name = "Apple Inc", Market = "stocks" };
        context.Tickers.Add(ticker);
        context.SaveChanges();

        var account = new Account
        {
            Id = Guid.NewGuid(),
            Name = "Test",
            Type = AccountType.Paper,
            InitialCash = 100_000m,
            Cash = 100_000m,
        };
        context.Accounts.Add(account);

        var execution = new StrategyExecution
        {
            TickerId = ticker.Id,
            StrategyName = "SMA Crossover",
            StartDate = "2025-01-01",
            EndDate = "2025-06-01",
            Timespan = "day",
            TotalTrades = tradeCount,
        };
        context.StrategyExecutions.Add(execution);
        context.SaveChanges();

        // Add backtest trades
        var baseTime = new DateTime(2025, 1, 15, 10, 0, 0, DateTimeKind.Utc);
        for (int i = 0; i < tradeCount; i++)
        {
            context.BacktestTrades.Add(new BacktestTrade
            {
                StrategyExecutionId = execution.Id,
                TradeType = "Buy",
                EntryTimestamp = baseTime.AddDays(i * 10),
                ExitTimestamp = baseTime.AddDays(i * 10 + 5),
                EntryPrice = 150m + i * 10,
                ExitPrice = 160m + i * 10,
                Quantity = 50,
                PnL = 500m,
                CumulativePnL = 500m * (i + 1),
            });
        }
        context.SaveChanges();

        return (account, ticker, execution);
    }

    #region ImportBacktestTrades

    [Fact]
    public async Task ImportBacktestTrades_CreatesPortfolioTradesAndLinks()
    {
        var (service, context) = CreateServiceWithContext();
        var (account, ticker, execution) = SeedFullSetup(context, tradeCount: 2);

        var trades = await service.ImportBacktestTradesAsync(execution.Id, account.Id);

        // 2 backtest trades → 2 buy + 2 sell = 4 portfolio trades
        Assert.Equal(4, trades.Count);

        // Verify strategy trade links created
        var links = context.StrategyTradeLinks.Where(l => l.StrategyExecutionId == execution.Id).ToList();
        Assert.Equal(4, links.Count);

        // Verify strategy allocation created
        var allocation = context.StrategyAllocations
            .FirstOrDefault(a => a.AccountId == account.Id && a.StrategyExecutionId == execution.Id);
        Assert.NotNull(allocation);
        Assert.Equal(100_000m, allocation.CapitalAllocated);

        // Verify positions were created via position engine
        var positions = context.Positions.Where(p => p.AccountId == account.Id).ToList();
        Assert.NotEmpty(positions);
    }

    #endregion

    #region GetStrategyPnL

    [Fact]
    public async Task GetStrategyPnL_ComputesFromLinkedLots()
    {
        var (service, context) = CreateServiceWithContext();
        var (account, ticker, execution) = SeedFullSetup(context, tradeCount: 1);

        // Import trades to create portfolio trades, links, and position lots
        await service.ImportBacktestTradesAsync(execution.Id, account.Id);

        var result = await service.GetStrategyPnLAsync(execution.Id);

        Assert.Equal(execution.Id, result.StrategyExecutionId);
        Assert.Equal("SMA Crossover", result.StrategyName);
        Assert.Equal(2, result.TradeCount); // 1 buy + 1 sell
        // PnL from FIFO: buy at 150, sell at 160, qty 50 → realized PnL = 500
        Assert.Equal(500m, result.TotalPnL);
        Assert.Equal(1m, result.WinRate); // 1 winning lot out of 1
    }

    #endregion

    #region GetAlphaAttribution

    [Fact]
    public async Task GetAlphaAttribution_MultipleStrategies_ComputesContribution()
    {
        var (service, context) = CreateServiceWithContext();
        var ticker = new Ticker { Symbol = "AAPL", Name = "Apple Inc", Market = "stocks" };
        context.Tickers.Add(ticker);
        context.SaveChanges();

        var account = new Account
        {
            Id = Guid.NewGuid(),
            Name = "Test",
            Type = AccountType.Paper,
            InitialCash = 200_000m,
            Cash = 200_000m,
        };
        context.Accounts.Add(account);

        // Strategy 1: SMA
        var exec1 = new StrategyExecution
        {
            TickerId = ticker.Id,
            StrategyName = "SMA",
            StartDate = "2025-01-01",
            EndDate = "2025-06-01",
            Timespan = "day",
            TotalTrades = 1,
        };
        context.StrategyExecutions.Add(exec1);
        context.SaveChanges();

        context.BacktestTrades.Add(new BacktestTrade
        {
            StrategyExecutionId = exec1.Id,
            TradeType = "Buy",
            EntryTimestamp = new DateTime(2025, 1, 10, 10, 0, 0, DateTimeKind.Utc),
            ExitTimestamp = new DateTime(2025, 1, 15, 10, 0, 0, DateTimeKind.Utc),
            EntryPrice = 100m,
            ExitPrice = 120m,
            Quantity = 50,
            PnL = 1000m,
        });
        context.SaveChanges();

        // Strategy 2: RSI
        var exec2 = new StrategyExecution
        {
            TickerId = ticker.Id,
            StrategyName = "RSI",
            StartDate = "2025-02-01",
            EndDate = "2025-06-01",
            Timespan = "day",
            TotalTrades = 1,
        };
        context.StrategyExecutions.Add(exec2);
        context.SaveChanges();

        context.BacktestTrades.Add(new BacktestTrade
        {
            StrategyExecutionId = exec2.Id,
            TradeType = "Buy",
            EntryTimestamp = new DateTime(2025, 2, 10, 10, 0, 0, DateTimeKind.Utc),
            ExitTimestamp = new DateTime(2025, 2, 15, 10, 0, 0, DateTimeKind.Utc),
            EntryPrice = 100m,
            ExitPrice = 110m,
            Quantity = 50,
            PnL = 500m,
        });
        context.SaveChanges();

        // Import both strategies
        await service.ImportBacktestTradesAsync(exec1.Id, account.Id);
        await service.ImportBacktestTradesAsync(exec2.Id, account.Id);

        var attributions = await service.GetAlphaAttributionAsync(account.Id);

        Assert.Equal(2, attributions.Count);

        var totalPnL = attributions.Sum(a => a.PnL);
        Assert.True(totalPnL > 0);

        // Verify contribution percentages sum to 1
        var totalContribution = attributions.Sum(a => a.ContributionPercent);
        Assert.True(Math.Abs(totalContribution - 1m) < 0.001m);

        // SMA had larger PnL → larger contribution
        var sma = attributions.First(a => a.StrategyName == "SMA");
        var rsi = attributions.First(a => a.StrategyName == "RSI");
        Assert.True(sma.ContributionPercent > rsi.ContributionPercent);
    }

    #endregion
}

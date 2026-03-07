using Backend.Models.MarketData;
using Backend.Models.Portfolio;
using Backend.Services.Implementation;
using Backend.Tests.Helpers;
using Microsoft.Extensions.Logging;
using Moq;

namespace Backend.Tests.Unit.Services;

public class PositionEngineTests
{
    private readonly Mock<ILogger<PositionEngine>> _loggerMock = new();

    private PositionEngine CreateEngine()
    {
        var context = TestDbContextFactory.Create();
        return new PositionEngine(context, _loggerMock.Object);
    }

    private static (PositionEngine engine, Backend.Data.AppDbContext context) CreateEngineWithContext()
    {
        var context = TestDbContextFactory.Create();
        var logger = new Mock<ILogger<PositionEngine>>();
        return (new PositionEngine(context, logger.Object), context);
    }

    private static Ticker SeedTicker(Backend.Data.AppDbContext context)
    {
        var ticker = new Ticker { Symbol = "AAPL", Name = "Apple Inc", Market = "stocks" };
        context.Tickers.Add(ticker);
        context.SaveChanges();
        return ticker;
    }

    private static Account SeedAccount(Backend.Data.AppDbContext context, decimal cash = 100_000m)
    {
        var account = new Account
        {
            Id = Guid.NewGuid(),
            Name = "Test Account",
            Type = AccountType.Paper,
            InitialCash = cash,
            Cash = cash,
        };
        context.Accounts.Add(account);
        context.SaveChanges();
        return account;
    }

    private static PortfolioTrade CreateTrade(Guid accountId, int tickerId, OrderSide side,
        decimal quantity, decimal price, int multiplier = 1, Guid? optionContractId = null,
        DateTime? timestamp = null)
    {
        var orderId = Guid.NewGuid();
        return new PortfolioTrade
        {
            Id = Guid.NewGuid(),
            AccountId = accountId,
            OrderId = orderId,
            TickerId = tickerId,
            Side = side,
            Quantity = quantity,
            Price = price,
            Fees = 0,
            AssetType = optionContractId.HasValue ? AssetType.Option : AssetType.Stock,
            OptionContractId = optionContractId,
            Multiplier = multiplier,
            ExecutionTimestamp = timestamp ?? DateTime.UtcNow,
        };
    }

    private static void SeedOrderForTrade(Backend.Data.AppDbContext context, PortfolioTrade trade)
    {
        var order = new Order
        {
            Id = trade.OrderId,
            AccountId = trade.AccountId,
            TickerId = trade.TickerId,
            Side = trade.Side,
            OrderType = OrderType.Market,
            Quantity = trade.Quantity,
            Status = OrderStatus.Filled,
            SubmittedAt = trade.ExecutionTimestamp,
            FilledAt = trade.ExecutionTimestamp,
        };
        context.Orders.Add(order);
        context.SaveChanges();
    }

    #region ApplyTrade — Single Buy

    [Fact]
    public async Task ApplyTrade_SingleBuy_CreatesPositionAndLot()
    {
        var (engine, context) = CreateEngineWithContext();
        var ticker = SeedTicker(context);
        var account = SeedAccount(context);

        var trade = CreateTrade(account.Id, ticker.Id, OrderSide.Buy, 100, 150m);
        SeedOrderForTrade(context, trade);
        context.PortfolioTrades.Add(trade);
        await context.SaveChangesAsync();

        var position = await engine.ApplyTradeAsync(trade);

        Assert.Equal(100m, position.NetQuantity);
        Assert.Equal(150m, position.AvgCostBasis);
        Assert.Equal(PositionStatus.Open, position.Status);
        Assert.Single(position.Lots);
        Assert.Equal(100m, position.Lots[0].RemainingQuantity);
        Assert.Null(position.Lots[0].ClosedAt);
    }

    #endregion

    #region ApplyTrade — FIFO Close

    [Fact]
    public async Task ApplyTrade_TwoBuysOneSell_FifoClosesFirstLot()
    {
        var (engine, context) = CreateEngineWithContext();
        var ticker = SeedTicker(context);
        var account = SeedAccount(context);
        var baseTime = new DateTime(2026, 1, 1, 10, 0, 0, DateTimeKind.Utc);

        // Buy 100 @ 150
        var trade1 = CreateTrade(account.Id, ticker.Id, OrderSide.Buy, 100, 150m, timestamp: baseTime);
        SeedOrderForTrade(context, trade1);
        context.PortfolioTrades.Add(trade1);
        await context.SaveChangesAsync();
        await engine.ApplyTradeAsync(trade1);

        // Buy 100 @ 160
        var trade2 = CreateTrade(account.Id, ticker.Id, OrderSide.Buy, 100, 160m, timestamp: baseTime.AddMinutes(1));
        SeedOrderForTrade(context, trade2);
        context.PortfolioTrades.Add(trade2);
        await context.SaveChangesAsync();
        await engine.ApplyTradeAsync(trade2);

        // Sell 100 @ 170 — should close first lot (FIFO)
        var trade3 = CreateTrade(account.Id, ticker.Id, OrderSide.Sell, 100, 170m, timestamp: baseTime.AddMinutes(2));
        SeedOrderForTrade(context, trade3);
        context.PortfolioTrades.Add(trade3);
        await context.SaveChangesAsync();
        var position = await engine.ApplyTradeAsync(trade3);

        // First lot closed: PnL = (170 - 150) * 100 = 2000
        Assert.Equal(100m, position.NetQuantity);
        Assert.Equal(2000m, position.RealizedPnL);
        Assert.Equal(PositionStatus.Open, position.Status);

        var closedLot = position.Lots.First(l => l.RemainingQuantity == 0);
        Assert.Equal(2000m, closedLot.RealizedPnL);
        Assert.NotNull(closedLot.ClosedAt);

        var openLot = position.Lots.First(l => l.RemainingQuantity > 0);
        Assert.Equal(100m, openLot.RemainingQuantity);
        Assert.Equal(160m, openLot.EntryPrice);
    }

    #endregion

    #region Full Close

    [Fact]
    public async Task ApplyTrade_FullClose_PositionStatusClosed()
    {
        var (engine, context) = CreateEngineWithContext();
        var ticker = SeedTicker(context);
        var account = SeedAccount(context);
        var baseTime = new DateTime(2026, 1, 1, 10, 0, 0, DateTimeKind.Utc);

        var buy = CreateTrade(account.Id, ticker.Id, OrderSide.Buy, 100, 150m, timestamp: baseTime);
        SeedOrderForTrade(context, buy);
        context.PortfolioTrades.Add(buy);
        await context.SaveChangesAsync();
        await engine.ApplyTradeAsync(buy);

        var sell = CreateTrade(account.Id, ticker.Id, OrderSide.Sell, 100, 170m, timestamp: baseTime.AddMinutes(1));
        SeedOrderForTrade(context, sell);
        context.PortfolioTrades.Add(sell);
        await context.SaveChangesAsync();
        var position = await engine.ApplyTradeAsync(sell);

        Assert.Equal(0m, position.NetQuantity);
        Assert.Equal(PositionStatus.Closed, position.Status);
        Assert.NotNull(position.ClosedAt);
        Assert.Equal(2000m, position.RealizedPnL); // (170-150) * 100
    }

    #endregion

    #region Partial Close

    [Fact]
    public async Task CloseLotsFifo_PartialClose_SplitsLot()
    {
        var (engine, context) = CreateEngineWithContext();
        var ticker = SeedTicker(context);
        var account = SeedAccount(context);
        var baseTime = new DateTime(2026, 1, 1, 10, 0, 0, DateTimeKind.Utc);

        var buy = CreateTrade(account.Id, ticker.Id, OrderSide.Buy, 100, 150m, timestamp: baseTime);
        SeedOrderForTrade(context, buy);
        context.PortfolioTrades.Add(buy);
        await context.SaveChangesAsync();
        await engine.ApplyTradeAsync(buy);

        // Sell only 50
        var sell = CreateTrade(account.Id, ticker.Id, OrderSide.Sell, 50, 170m, timestamp: baseTime.AddMinutes(1));
        SeedOrderForTrade(context, sell);
        context.PortfolioTrades.Add(sell);
        await context.SaveChangesAsync();
        var position = await engine.ApplyTradeAsync(sell);

        Assert.Equal(50m, position.NetQuantity);
        Assert.Equal(PositionStatus.Open, position.Status);

        var lot = position.Lots[0];
        Assert.Equal(50m, lot.RemainingQuantity);
        Assert.Equal(1000m, lot.RealizedPnL); // (170-150) * 50
        Assert.Null(lot.ClosedAt); // partially open
    }

    #endregion

    #region Option Trade — Multiplier

    [Fact]
    public async Task ApplyTrade_OptionTrade_MultiplierApplied()
    {
        var (engine, context) = CreateEngineWithContext();
        var ticker = SeedTicker(context);
        var account = SeedAccount(context);
        var baseTime = new DateTime(2026, 1, 1, 10, 0, 0, DateTimeKind.Utc);

        var contract = new OptionContract
        {
            Id = Guid.NewGuid(),
            UnderlyingTickerId = ticker.Id,
            Symbol = "O:AAPL250620C00150000",
            Strike = 150m,
            Expiration = new DateOnly(2025, 6, 20),
            OptionType = Backend.Models.Portfolio.OptionType.Call,
            Multiplier = 100,
        };
        context.OptionContracts.Add(contract);
        await context.SaveChangesAsync();

        // Buy 1 contract @ 5.00, multiplier = 100
        var buy = CreateTrade(account.Id, ticker.Id, OrderSide.Buy, 1, 5.00m,
            multiplier: 100, optionContractId: contract.Id, timestamp: baseTime);
        SeedOrderForTrade(context, buy);
        context.PortfolioTrades.Add(buy);
        await context.SaveChangesAsync();
        await engine.ApplyTradeAsync(buy);

        // Sell 1 contract @ 8.00
        var sell = CreateTrade(account.Id, ticker.Id, OrderSide.Sell, 1, 8.00m,
            multiplier: 100, optionContractId: contract.Id, timestamp: baseTime.AddMinutes(1));
        SeedOrderForTrade(context, sell);
        context.PortfolioTrades.Add(sell);
        await context.SaveChangesAsync();
        var position = await engine.ApplyTradeAsync(sell);

        // PnL = (8.00 - 5.00) * 1 * 100 = 300
        Assert.Equal(300m, position.RealizedPnL);
        Assert.Equal(PositionStatus.Closed, position.Status);
    }

    #endregion

    #region CalculateRealizedPnL

    [Fact]
    public void CalculateRealizedPnL_MultipleLots_SumsCorrectly()
    {
        var engine = CreateEngine();

        var lots = new List<PositionLot>
        {
            new() { RealizedPnL = 500m },
            new() { RealizedPnL = -200m },
            new() { RealizedPnL = 1000m },
        };

        var total = engine.CalculateRealizedPnL(lots);
        Assert.Equal(1300m, total);
    }

    #endregion

    #region Rebuild Positions

    [Fact]
    public async Task RebuildPositions_ReplaysTrades_MatchesIncrementalState()
    {
        var (engine, context) = CreateEngineWithContext();
        var ticker = SeedTicker(context);
        var account = SeedAccount(context);
        var baseTime = new DateTime(2026, 1, 1, 10, 0, 0, DateTimeKind.Utc);

        // Apply trades incrementally
        var trades = new List<PortfolioTrade>();
        for (int i = 0; i < 5; i++)
        {
            var trade = CreateTrade(account.Id, ticker.Id, OrderSide.Buy, 10, 150m + i,
                timestamp: baseTime.AddMinutes(i));
            SeedOrderForTrade(context, trade);
            context.PortfolioTrades.Add(trade);
            await context.SaveChangesAsync();
            trades.Add(trade);
        }

        // Apply incrementally
        Position? incrementalPosition = null;
        foreach (var trade in trades)
        {
            incrementalPosition = await engine.ApplyTradeAsync(trade);
        }

        // Capture incremental state
        var incrementalQty = incrementalPosition!.NetQuantity;
        var incrementalCost = incrementalPosition.AvgCostBasis;

        // Rebuild from scratch
        var rebuiltPositions = await engine.RebuildPositionsAsync(account.Id);

        Assert.Single(rebuiltPositions);
        Assert.Equal(incrementalQty, rebuiltPositions[0].NetQuantity);
        // Allow small rounding tolerance on avg cost
        Assert.Equal(incrementalCost, rebuiltPositions[0].AvgCostBasis, 4);
    }

    #endregion
}

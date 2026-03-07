using Backend.Models.MarketData;
using Backend.Models.Portfolio;
using Backend.Services.Implementation;
using Backend.Services.Interfaces;
using Backend.Tests.Helpers;
using Microsoft.Extensions.Logging;
using Moq;

namespace Backend.Tests.Unit.Services;

public class PortfolioServiceTests
{
    private static (PortfolioService service, Backend.Data.AppDbContext context) CreateServiceWithContext()
    {
        var context = TestDbContextFactory.Create();
        var engineLogger = new Mock<ILogger<PositionEngine>>();
        var serviceLogger = new Mock<ILogger<PortfolioService>>();
        var engine = new PositionEngine(context, engineLogger.Object);
        var service = new PortfolioService(context, engine, serviceLogger.Object);
        return (service, context);
    }

    private static Ticker SeedTicker(Backend.Data.AppDbContext context)
    {
        var ticker = new Ticker { Symbol = "AAPL", Name = "Apple Inc", Market = "stocks" };
        context.Tickers.Add(ticker);
        context.SaveChanges();
        return ticker;
    }

    #region CreateAccount

    [Fact]
    public async Task CreateAccount_ReturnsAccountWithCorrectCash()
    {
        var (service, _) = CreateServiceWithContext();

        var account = await service.CreateAccountAsync("Test", AccountType.Paper, 100_000m);

        Assert.NotEqual(Guid.Empty, account.Id);
        Assert.Equal("Test", account.Name);
        Assert.Equal(AccountType.Paper, account.Type);
        Assert.Equal(100_000m, account.Cash);
        Assert.Equal(100_000m, account.InitialCash);
    }

    #endregion

    #region SubmitOrder / CancelOrder

    [Fact]
    public async Task SubmitOrder_CreatesOrderWithPendingStatus()
    {
        var (service, context) = CreateServiceWithContext();
        var ticker = SeedTicker(context);
        var account = await service.CreateAccountAsync("Test", AccountType.Paper, 100_000m);

        var order = await service.SubmitOrderAsync(
            account.Id, ticker.Id, OrderSide.Buy, OrderType.Market, 100, null);

        Assert.Equal(OrderStatus.Pending, order.Status);
        Assert.Equal(100m, order.Quantity);
        Assert.Equal(OrderSide.Buy, order.Side);
    }

    [Fact]
    public async Task CancelOrder_SetsCancelledStatus()
    {
        var (service, context) = CreateServiceWithContext();
        var ticker = SeedTicker(context);
        var account = await service.CreateAccountAsync("Test", AccountType.Paper, 100_000m);

        var order = await service.SubmitOrderAsync(
            account.Id, ticker.Id, OrderSide.Buy, OrderType.Limit, 100, 150m);
        var cancelled = await service.CancelOrderAsync(order.Id);

        Assert.Equal(OrderStatus.Cancelled, cancelled.Status);
    }

    [Fact]
    public async Task CancelOrder_AlreadyFilled_Throws()
    {
        var (service, context) = CreateServiceWithContext();
        var ticker = SeedTicker(context);
        var account = await service.CreateAccountAsync("Test", AccountType.Paper, 100_000m);

        var order = await service.SubmitOrderAsync(
            account.Id, ticker.Id, OrderSide.Buy, OrderType.Market, 100, null);
        await service.FillOrderAsync(order.Id, 150m, 100m);

        await Assert.ThrowsAsync<InvalidOperationException>(
            () => service.CancelOrderAsync(order.Id));
    }

    #endregion

    #region FillOrder — Cash Impact

    [Fact]
    public async Task FillOrder_Buy_DeductsCashCorrectly()
    {
        var (service, context) = CreateServiceWithContext();
        var ticker = SeedTicker(context);
        var account = await service.CreateAccountAsync("Test", AccountType.Paper, 100_000m);

        var order = await service.SubmitOrderAsync(
            account.Id, ticker.Id, OrderSide.Buy, OrderType.Market, 100, null);
        await service.FillOrderAsync(order.Id, 150m, 100m, fees: 10m);

        // Reload account
        var updatedAccount = await context.Accounts.FindAsync(account.Id);
        // Cash = 100_000 - (150 * 100 * 1) - 10 = 84_990
        Assert.Equal(84_990m, updatedAccount!.Cash);
    }

    [Fact]
    public async Task FillOrder_Sell_AddsCashCorrectly()
    {
        var (service, context) = CreateServiceWithContext();
        var ticker = SeedTicker(context);
        var account = await service.CreateAccountAsync("Test", AccountType.Paper, 100_000m);

        // Buy first
        var buyOrder = await service.SubmitOrderAsync(
            account.Id, ticker.Id, OrderSide.Buy, OrderType.Market, 100, null);
        await service.FillOrderAsync(buyOrder.Id, 150m, 100m);

        // Then sell
        var sellOrder = await service.SubmitOrderAsync(
            account.Id, ticker.Id, OrderSide.Sell, OrderType.Market, 100, null);
        await service.FillOrderAsync(sellOrder.Id, 170m, 100m, fees: 10m);

        var updatedAccount = await context.Accounts.FindAsync(account.Id);
        // Cash = 100_000 - 15_000 + (170 * 100) - 10 = 101_990
        Assert.Equal(101_990m, updatedAccount!.Cash);
    }

    #endregion

    #region RecordTrade

    [Fact]
    public async Task RecordTrade_CreatesTradeAndPosition()
    {
        var (service, context) = CreateServiceWithContext();
        var ticker = SeedTicker(context);
        var account = await service.CreateAccountAsync("Test", AccountType.Paper, 100_000m);

        var input = new RecordTradeInput
        {
            AccountId = account.Id,
            TickerId = ticker.Id,
            Side = OrderSide.Buy,
            Quantity = 100,
            Price = 150m,
            Fees = 0,
            Multiplier = 1,
        };

        var trade = await service.RecordTradeAsync(input);

        Assert.NotEqual(Guid.Empty, trade.Id);
        Assert.Equal(150m, trade.Price);
        Assert.Equal(100m, trade.Quantity);
    }

    [Fact]
    public async Task RecordTrade_OptionWithGreeks_CreatesOptionLeg()
    {
        var (service, context) = CreateServiceWithContext();
        var ticker = SeedTicker(context);
        var account = await service.CreateAccountAsync("Test", AccountType.Paper, 100_000m);

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

        var input = new RecordTradeInput
        {
            AccountId = account.Id,
            TickerId = ticker.Id,
            Side = OrderSide.Buy,
            Quantity = 10,
            Price = 5.00m,
            Fees = 0,
            AssetType = AssetType.Option,
            OptionContractId = contract.Id,
            Multiplier = 100,
            OptionLeg = new OptionLegInput
            {
                OptionContractId = contract.Id,
                Quantity = 10,
                EntryIV = 0.30m,
                EntryDelta = 0.55m,
                EntryGamma = 0.03m,
                EntryTheta = -0.05m,
                EntryVega = 0.15m,
            },
        };

        var trade = await service.RecordTradeAsync(input);

        var leg = context.OptionLegs.FirstOrDefault(l => l.TradeId == trade.Id);
        Assert.NotNull(leg);
        Assert.Equal(0.30m, leg.EntryIV);
        Assert.Equal(0.55m, leg.EntryDelta);
        Assert.Equal(0.03m, leg.EntryGamma);
        Assert.Equal(-0.05m, leg.EntryTheta);
        Assert.Equal(0.15m, leg.EntryVega);
    }

    #endregion

    #region GetPortfolioState

    [Fact]
    public async Task GetPortfolioState_ReturnsAccountAndPositions()
    {
        var (service, context) = CreateServiceWithContext();
        var ticker = SeedTicker(context);
        var account = await service.CreateAccountAsync("Test", AccountType.Paper, 100_000m);

        var input = new RecordTradeInput
        {
            AccountId = account.Id,
            TickerId = ticker.Id,
            Side = OrderSide.Buy,
            Quantity = 100,
            Price = 150m,
            Fees = 0,
            Multiplier = 1,
        };
        await service.RecordTradeAsync(input);

        var state = await service.GetPortfolioStateAsync(account.Id);

        Assert.Equal(account.Id, state.Account.Id);
        Assert.Single(state.Positions);
        Assert.Equal(100m, state.Positions[0].NetQuantity);
    }

    #endregion
}

using Backend.Models.MarketData;
using Backend.Models.Portfolio;
using Backend.Services.Implementation;
using Backend.Services.Interfaces;
using Backend.Tests.Helpers;
using Microsoft.Extensions.Logging;
using Moq;

namespace Backend.Tests.Unit.Services;

public class PortfolioValuationServiceTests
{
    private static (PortfolioValuationService service, Backend.Data.AppDbContext context) CreateServiceWithContext()
    {
        var context = TestDbContextFactory.Create();
        var polygonService = new Mock<IPolygonService>();
        var logger = new Mock<ILogger<PortfolioValuationService>>();
        var service = new PortfolioValuationService(context, polygonService.Object, logger.Object);
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

    private static async Task CreatePosition(Backend.Data.AppDbContext context,
        Guid accountId, int tickerId, decimal quantity, decimal avgCost,
        AssetType assetType = AssetType.Stock, Guid? optionContractId = null)
    {
        var position = new Position
        {
            Id = Guid.NewGuid(),
            AccountId = accountId,
            TickerId = tickerId,
            AssetType = assetType,
            OptionContractId = optionContractId,
            NetQuantity = quantity,
            AvgCostBasis = avgCost,
            Status = PositionStatus.Open,
            OpenedAt = DateTime.UtcNow,
            LastUpdated = DateTime.UtcNow,
        };
        context.Positions.Add(position);
        await context.SaveChangesAsync();
    }

    #region ComputeMarketValue — Stocks

    [Fact]
    public async Task ComputeValuation_StockPosition_PriceTimesQuantity()
    {
        var (service, context) = CreateServiceWithContext();
        var (account, ticker) = SeedAccountAndTicker(context, 50_000m);

        await CreatePosition(context, account.Id, ticker.Id, 100, 150m);

        var prices = new Dictionary<string, decimal> { ["AAPL"] = 175m };
        var valuation = await service.ComputeValuationWithPricesAsync(account.Id, prices);

        Assert.Equal(17_500m, valuation.MarketValue); // 175 * 100
        Assert.Equal(67_500m, valuation.Equity);      // 50_000 + 17_500
    }

    #endregion

    #region ComputeMarketValue — Options with Multiplier

    [Fact]
    public async Task ComputeValuation_OptionPosition_MultiplierApplied()
    {
        var (service, context) = CreateServiceWithContext();
        var (account, ticker) = SeedAccountAndTicker(context, 50_000m);

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

        await CreatePosition(context, account.Id, ticker.Id, 10, 5m,
            AssetType.Option, contract.Id);

        var prices = new Dictionary<string, decimal> { ["AAPL"] = 5.50m };
        var valuation = await service.ComputeValuationWithPricesAsync(account.Id, prices);

        // MarketValue = 5.50 * 10 * 100 = 5500
        Assert.Equal(5_500m, valuation.MarketValue);
    }

    #endregion

    #region UnrealizedPnL

    [Fact]
    public async Task ComputeValuation_UnrealizedPnL_CorrectDelta()
    {
        var (service, context) = CreateServiceWithContext();
        var (account, ticker) = SeedAccountAndTicker(context, 50_000m);

        await CreatePosition(context, account.Id, ticker.Id, 100, 150m);

        var prices = new Dictionary<string, decimal> { ["AAPL"] = 175m };
        var valuation = await service.ComputeValuationWithPricesAsync(account.Id, prices);

        // UnrealizedPnL = (175 - 150) * 100 = 2500
        Assert.Equal(2_500m, valuation.UnrealizedPnL);
    }

    #endregion

    #region Equity = Cash + MarketValue

    [Fact]
    public async Task ComputeValuation_EquityCashPlusMarketValue()
    {
        var (service, context) = CreateServiceWithContext();
        var (account, ticker) = SeedAccountAndTicker(context, 50_000m);

        await CreatePosition(context, account.Id, ticker.Id, 100, 150m);

        var prices = new Dictionary<string, decimal> { ["AAPL"] = 175m };
        var valuation = await service.ComputeValuationWithPricesAsync(account.Id, prices);

        Assert.Equal(50_000m, valuation.Cash);
        Assert.Equal(17_500m, valuation.MarketValue);
        Assert.Equal(67_500m, valuation.Equity); // 50k + 17.5k
    }

    #endregion

    #region No Positions

    [Fact]
    public async Task ComputeValuation_NoPositions_EquityEqualsCash()
    {
        var (service, context) = CreateServiceWithContext();
        var (account, _) = SeedAccountAndTicker(context, 100_000m);

        var prices = new Dictionary<string, decimal>();
        var valuation = await service.ComputeValuationWithPricesAsync(account.Id, prices);

        Assert.Equal(100_000m, valuation.Equity);
        Assert.Equal(0m, valuation.MarketValue);
        Assert.Equal(0m, valuation.UnrealizedPnL);
    }

    #endregion

    #region Multiple Positions

    [Fact]
    public async Task ComputeValuation_MultiplePositions_AggregatesCorrectly()
    {
        var (service, context) = CreateServiceWithContext();
        var (account, aaplTicker) = SeedAccountAndTicker(context, 50_000m);

        var msftTicker = new Ticker { Symbol = "MSFT", Name = "Microsoft", Market = "stocks" };
        context.Tickers.Add(msftTicker);
        await context.SaveChangesAsync();

        await CreatePosition(context, account.Id, aaplTicker.Id, 100, 150m);
        await CreatePosition(context, account.Id, msftTicker.Id, 50, 400m);

        var prices = new Dictionary<string, decimal>
        {
            ["AAPL"] = 175m,
            ["MSFT"] = 420m,
        };

        var valuation = await service.ComputeValuationWithPricesAsync(account.Id, prices);

        // AAPL: 175 * 100 = 17500, MSFT: 420 * 50 = 21000
        Assert.Equal(38_500m, valuation.MarketValue);
        Assert.Equal(2, valuation.Positions.Count);
    }

    #endregion
}

using Backend.Models.MarketData;
using Backend.Models.Portfolio;
using Backend.Services.Implementation;
using Backend.Services.Interfaces;
using Backend.Tests.Helpers;
using Microsoft.Extensions.Logging;
using Moq;

namespace Backend.Tests.Unit.Services;

public class PortfolioRiskServiceTests
{
    private static (PortfolioRiskService service, Backend.Data.AppDbContext context, Mock<IPortfolioValuationService> valuationMock) CreateServiceWithContext()
    {
        var context = TestDbContextFactory.Create();
        var valuationMock = new Mock<IPortfolioValuationService>();
        var logger = new Mock<ILogger<PortfolioRiskService>>();
        var service = new PortfolioRiskService(context, valuationMock.Object, logger.Object);
        return (service, context, valuationMock);
    }

    private static (Account account, Ticker ticker) SeedAccountAndTicker(
        Backend.Data.AppDbContext context, decimal cash = 100_000m)
    {
        var ticker = new Ticker { Symbol = "SPY", Name = "SPDR S&P 500", Market = "stocks" };
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

    private static Position CreateOpenPosition(Backend.Data.AppDbContext context,
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
        context.SaveChanges();
        return position;
    }

    #region DollarDelta — Stocks

    [Fact]
    public async Task ComputeDollarDelta_Stock_DeltaIsOne()
    {
        var (service, context, _) = CreateServiceWithContext();
        var (account, ticker) = SeedAccountAndTicker(context);

        CreateOpenPosition(context, account.Id, ticker.Id, 100, 450m);

        var prices = new Dictionary<string, decimal> { ["SPY"] = 500m };
        var results = await service.ComputeDollarDeltaAsync(account.Id, prices);

        Assert.Single(results);
        Assert.Equal(1m, results[0].Delta);
        Assert.Equal(50_000m, results[0].DollarDelta); // 1 * 500 * 100 * 1
    }

    #endregion

    #region DollarDelta — Options

    [Fact]
    public async Task ComputeDollarDelta_Option_UsesLatestLegDelta()
    {
        var (service, context, _) = CreateServiceWithContext();
        var (account, ticker) = SeedAccountAndTicker(context);

        var contract = new OptionContract
        {
            Id = Guid.NewGuid(),
            UnderlyingTickerId = ticker.Id,
            Symbol = "O:SPY250620C00500000",
            Strike = 500m,
            Expiration = new DateOnly(2025, 6, 20),
            OptionType = Backend.Models.Portfolio.OptionType.Call,
            Multiplier = 100,
        };
        context.OptionContracts.Add(contract);
        context.SaveChanges();

        var position = CreateOpenPosition(context, account.Id, ticker.Id, 10, 5m,
            AssetType.Option, contract.Id);

        // Create a trade with an option leg that has delta = 0.65
        var order = new Order
        {
            Id = Guid.NewGuid(),
            AccountId = account.Id,
            TickerId = ticker.Id,
            Side = OrderSide.Buy,
            OrderType = OrderType.Market,
            Quantity = 10,
            Status = OrderStatus.Filled,
        };
        context.Orders.Add(order);

        var trade = new PortfolioTrade
        {
            Id = Guid.NewGuid(),
            AccountId = account.Id,
            OrderId = order.Id,
            TickerId = ticker.Id,
            Side = OrderSide.Buy,
            Quantity = 10,
            Price = 5m,
            Fees = 0,
            Multiplier = 100,
            ExecutionTimestamp = DateTime.UtcNow,
            OptionContractId = contract.Id,
        };
        context.PortfolioTrades.Add(trade);

        var leg = new OptionLeg
        {
            Id = Guid.NewGuid(),
            TradeId = trade.Id,
            OptionContractId = contract.Id,
            EntryDelta = 0.65m,
            EntryIV = 0.25m,
        };
        context.OptionLegs.Add(leg);
        context.SaveChanges();

        var prices = new Dictionary<string, decimal> { ["SPY"] = 505m };
        var results = await service.ComputeDollarDeltaAsync(account.Id, prices);

        Assert.Single(results);
        Assert.Equal(0.65m, results[0].Delta);
        // DollarDelta = 0.65 * 505 * 10 * 100 = 328,250
        Assert.Equal(328_250m, results[0].DollarDelta);
    }

    #endregion

    #region EvaluateRiskRules — Triggered

    [Fact]
    public async Task EvaluateRiskRules_MaxDrawdownExceeded_ReturnsViolation()
    {
        var (service, context, valuationMock) = CreateServiceWithContext();
        var (account, ticker) = SeedAccountAndTicker(context, 100_000m);

        // Add snapshots with peak equity at 100k
        context.PortfolioSnapshots.Add(new PortfolioSnapshot
        {
            Id = Guid.NewGuid(),
            AccountId = account.Id,
            Timestamp = DateTime.UtcNow.AddDays(-1),
            Equity = 100_000m,
            Cash = 100_000m,
        });
        context.SaveChanges();

        // Current valuation shows equity dropped to 85k (15% drawdown)
        valuationMock.Setup(v => v.ComputeValuationWithPricesAsync(
                account.Id, It.IsAny<Dictionary<string, decimal>>(), It.IsAny<CancellationToken>()))
            .ReturnsAsync(new PortfolioValuation
            {
                Equity = 85_000m,
                Cash = 85_000m,
                Positions = [],
            });

        // Rule: max 10% drawdown
        context.RiskRules.Add(new RiskRule
        {
            Id = Guid.NewGuid(),
            AccountId = account.Id,
            RuleType = RiskRuleType.MaxDrawdown,
            Threshold = 0.10m,
            Action = RiskAction.Warn,
            Severity = RiskSeverity.High,
            Enabled = true,
        });
        context.SaveChanges();

        var violations = await service.EvaluateRiskRulesAsync(account.Id, new Dictionary<string, decimal>());

        Assert.Single(violations);
        Assert.Equal(RiskRuleType.MaxDrawdown, violations[0].RuleType);
        Assert.True(violations[0].ActualValue > 0.10m);
    }

    #endregion

    #region EvaluateRiskRules — Disabled

    [Fact]
    public async Task EvaluateRiskRules_DisabledRule_IsSkipped()
    {
        var (service, context, _) = CreateServiceWithContext();
        var (account, _) = SeedAccountAndTicker(context);

        context.RiskRules.Add(new RiskRule
        {
            Id = Guid.NewGuid(),
            AccountId = account.Id,
            RuleType = RiskRuleType.MaxDrawdown,
            Threshold = 0.01m, // very tight, would normally trigger
            Action = RiskAction.Block,
            Severity = RiskSeverity.Critical,
            Enabled = false,
        });
        context.SaveChanges();

        var violations = await service.EvaluateRiskRulesAsync(account.Id, new Dictionary<string, decimal>());

        Assert.Empty(violations);
    }

    #endregion

    #region RunScenario — Price Shock

    [Fact]
    public async Task RunScenario_PriceDown10Percent_EquityDrops()
    {
        var (service, context, valuationMock) = CreateServiceWithContext();
        var (account, ticker) = SeedAccountAndTicker(context, 50_000m);

        valuationMock.Setup(v => v.ComputeValuationWithPricesAsync(
                account.Id, It.IsAny<Dictionary<string, decimal>>(), It.IsAny<CancellationToken>()))
            .ReturnsAsync(new PortfolioValuation
            {
                Cash = 50_000m,
                MarketValue = 50_000m,
                Equity = 100_000m,
                Positions =
                [
                    new PositionValuation
                    {
                        Symbol = "SPY",
                        CurrentPrice = 500m,
                        Quantity = 100,
                        Multiplier = 1,
                        MarketValue = 50_000m,
                    }
                ],
            });

        var prices = new Dictionary<string, decimal> { ["SPY"] = 500m };
        var scenario = new ScenarioInput { PriceChangePercent = -0.10m };

        var result = await service.RunScenarioAsync(account.Id, prices, scenario);

        // SPY goes from 500 to 450 → market value = 450 * 100 = 45,000
        Assert.Equal(100_000m, result.CurrentEquity);
        Assert.Equal(95_000m, result.ScenarioEquity); // 50k cash + 45k market
        Assert.Equal(-5_000m, result.PnLImpact);
    }

    #endregion

    #region RunScenario — IV Change (Vega)

    [Fact]
    public async Task RunScenario_IVUp_OptionValueIncreasesViaVega()
    {
        var (service, context, valuationMock) = CreateServiceWithContext();
        var (account, ticker) = SeedAccountAndTicker(context, 90_000m);

        var contract = new OptionContract
        {
            Id = Guid.NewGuid(),
            UnderlyingTickerId = ticker.Id,
            Symbol = "O:SPY250620C00500000",
            Strike = 500m,
            Expiration = new DateOnly(2025, 6, 20),
            OptionType = Backend.Models.Portfolio.OptionType.Call,
            Multiplier = 100,
        };
        context.OptionContracts.Add(contract);

        // Create trade + leg with vega
        var order = new Order
        {
            Id = Guid.NewGuid(),
            AccountId = account.Id,
            TickerId = ticker.Id,
            Side = OrderSide.Buy,
            OrderType = OrderType.Market,
            Quantity = 5,
            Status = OrderStatus.Filled,
        };
        context.Orders.Add(order);

        var trade = new PortfolioTrade
        {
            Id = Guid.NewGuid(),
            AccountId = account.Id,
            OrderId = order.Id,
            TickerId = ticker.Id,
            Side = OrderSide.Buy,
            Quantity = 5,
            Price = 10m,
            Fees = 0,
            Multiplier = 100,
            ExecutionTimestamp = DateTime.UtcNow,
            OptionContractId = contract.Id,
        };
        context.PortfolioTrades.Add(trade);

        var leg = new OptionLeg
        {
            Id = Guid.NewGuid(),
            TradeId = trade.Id,
            OptionContractId = contract.Id,
            EntryVega = 0.20m,
            EntryDelta = 0.5m,
            EntryIV = 0.25m,
        };
        context.OptionLegs.Add(leg);
        context.SaveChanges();

        valuationMock.Setup(v => v.ComputeValuationWithPricesAsync(
                account.Id, It.IsAny<Dictionary<string, decimal>>(), It.IsAny<CancellationToken>()))
            .ReturnsAsync(new PortfolioValuation
            {
                Cash = 90_000m,
                MarketValue = 5_000m, // 5 contracts * 10 premium * 100 multiplier
                Equity = 95_000m,
                Positions =
                [
                    new PositionValuation
                    {
                        Symbol = "SPY",
                        CurrentPrice = 10m,
                        Quantity = 5,
                        Multiplier = 100,
                        MarketValue = 5_000m,
                    }
                ],
            });

        var prices = new Dictionary<string, decimal> { ["SPY"] = 10m };
        var scenario = new ScenarioInput { IvChangePercent = 0.05m }; // IV up 5%

        var result = await service.RunScenarioAsync(account.Id, prices, scenario);

        // Vega impact = 0.20 * 0.05 * 5 * 100 = 5
        // Scenario market value = 5000 + 5 = 5005
        Assert.True(result.ScenarioEquity > result.CurrentEquity);
        Assert.True(result.PnLImpact > 0);
    }

    #endregion

    #region PortfolioVega

    [Fact]
    public async Task ComputePortfolioVega_AggregatesAcrossPositions()
    {
        var (service, context, _) = CreateServiceWithContext();
        var (account, ticker) = SeedAccountAndTicker(context);

        var contract = new OptionContract
        {
            Id = Guid.NewGuid(),
            UnderlyingTickerId = ticker.Id,
            Symbol = "O:SPY250620C00500000",
            Strike = 500m,
            Expiration = new DateOnly(2025, 6, 20),
            OptionType = Backend.Models.Portfolio.OptionType.Call,
            Multiplier = 100,
        };
        context.OptionContracts.Add(contract);

        CreateOpenPosition(context, account.Id, ticker.Id, 10, 5m,
            AssetType.Option, contract.Id);

        var order = new Order
        {
            Id = Guid.NewGuid(),
            AccountId = account.Id,
            TickerId = ticker.Id,
            Side = OrderSide.Buy,
            OrderType = OrderType.Market,
            Quantity = 10,
            Status = OrderStatus.Filled,
        };
        context.Orders.Add(order);

        var trade = new PortfolioTrade
        {
            Id = Guid.NewGuid(),
            AccountId = account.Id,
            OrderId = order.Id,
            TickerId = ticker.Id,
            Side = OrderSide.Buy,
            Quantity = 10,
            Price = 5m,
            Fees = 0,
            Multiplier = 100,
            ExecutionTimestamp = DateTime.UtcNow,
            OptionContractId = contract.Id,
        };
        context.PortfolioTrades.Add(trade);

        var leg = new OptionLeg
        {
            Id = Guid.NewGuid(),
            TradeId = trade.Id,
            OptionContractId = contract.Id,
            EntryVega = 0.15m,
            EntryDelta = 0.5m,
            EntryIV = 0.20m,
        };
        context.OptionLegs.Add(leg);
        context.SaveChanges();

        var vega = await service.ComputePortfolioVegaAsync(account.Id);

        // totalVega = 0.15 * 10 * 100 = 150
        Assert.Equal(150m, vega);
    }

    #endregion
}

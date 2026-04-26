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
    private static (PortfolioRiskService service, Backend.Data.AppDbContext context, Mock<IPortfolioValuationService> valuationMock, Mock<IPolygonService> polygonMock) CreateServiceWithContext()
    {
        var context = TestDbContextFactory.Create();
        var valuationMock = new Mock<IPortfolioValuationService>();
        var polygonMock = new Mock<IPolygonService>();
        var logger = new Mock<ILogger<PortfolioRiskService>>();
        var service = new PortfolioRiskService(context, valuationMock.Object, polygonMock.Object, logger.Object);
        return (service, context, valuationMock, polygonMock);
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
        var (service, context, _, _) = CreateServiceWithContext();
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
    public async Task ComputeDollarDelta_Option_UsesLiveGreeksFromPython()
    {
        var (service, context, _, polygonMock) = CreateServiceWithContext();
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

        // Create a trade with an option leg. EntryDelta on the leg is no
        // longer read by ComputeDollarDeltaAsync — kept here only because the
        // model is non-nullable and test fidelity expects realistic data.
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
            EntryDelta = 0.50m, // intentionally different from the live-Greeks mock
            EntryIV = 0.25m,
        };
        context.OptionLegs.Add(leg);
        context.SaveChanges();

        // Python returns live-recomputed delta = 0.65 for this position.
        polygonMock
            .Setup(p => p.PortfolioLiveGreeksAsync(
                It.IsAny<long>(), It.IsAny<decimal>(),
                It.Is<List<Backend.Models.DTOs.PolygonResponses.PortfolioScenarioPositionDto>>(
                    list => list.Count == 1 && list[0].LegId == position.Id.ToString()),
                It.IsAny<decimal>(), It.IsAny<decimal>(), It.IsAny<CancellationToken>()))
            .ReturnsAsync(new Backend.Models.DTOs.PolygonResponses.PortfolioScenarioResponseDto
            {
                Symbol = "SPY",
                SpotPrice = 505m,
                Points =
                [
                    new Backend.Models.DTOs.PolygonResponses.ScenarioPointDto
                    {
                        Spot = 505m,
                        Legs =
                        [
                            new Backend.Models.DTOs.PolygonResponses.LegGreeksDto
                            {
                                LegId = position.Id.ToString(),
                                Instrument = "option",
                                Delta = 0.65m,
                            }
                        ],
                    }
                ],
            });

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
        var (service, context, valuationMock, _) = CreateServiceWithContext();
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
        var (service, context, _, _) = CreateServiceWithContext();
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

    #region RunScenario — Python passthrough (Phase 2.2 of migration plan)

    // The two old tests in this region (RunScenario_PriceDown10Percent_EquityDrops,
    // RunScenario_IVUp_OptionValueIncreasesViaVega) validated the *removed*
    // stale-Greek shock-propagation behavior. They have been replaced with
    // tests that mock the IPolygonService passthrough. The behavior under
    // test is now: "given a portfolio and a scenario input, .NET projects
    // positions into the Python request and aggregates the response correctly".
    // The math itself (recompute-Greeks-at-scenario-time) is verified by
    // PythonDataService/tests/services/test_portfolio_scenario.py.

    [Fact]
    public async Task RunScenario_StockPosition_PassesPositionToPythonAndAggregates()
    {
        var (service, context, valuationMock, polygonMock) = CreateServiceWithContext();
        var (account, ticker) = SeedAccountAndTicker(context, 50_000m);

        // Seed an open stock position in the DB so RunScenarioAsync's Python
        // projection has something to send.
        var position = CreateOpenPosition(context, account.Id, ticker.Id, 100, 500m,
            AssetType.Stock, optionContractId: null);
        var positionLegId = position.Id.ToString();

        valuationMock.Setup(v => v.ComputeValuationWithPricesAsync(
                account.Id, It.IsAny<Dictionary<string, decimal>>(), It.IsAny<CancellationToken>()))
            .ReturnsAsync(new PortfolioValuation
            {
                Cash = 50_000m,
                MarketValue = 50_000m,
                Equity = 100_000m,
                Positions =
                [
                    new PositionValuation { PositionId = position.Id, Symbol = "SPY", CurrentPrice = 500m, Quantity = 100, Multiplier = 1, MarketValue = 50_000m }
                ],
            });

        // Python returns the recomputed scenario point. Mock returns a single
        // ScenarioPoint with a stock leg at theoretical_price = 450 (i.e. -10%).
        // LegId echoes the position id so RunScenarioAsync's identity-based
        // join (Phase 2.2 round-2 fix) finds the leg.
        polygonMock
            .Setup(p => p.PortfolioScenarioAsync(
                It.IsAny<long>(), It.IsAny<decimal>(),
                It.IsAny<List<Backend.Models.DTOs.PolygonResponses.PortfolioScenarioPositionDto>>(),
                It.IsAny<Backend.Models.DTOs.PolygonResponses.PortfolioScenarioGridDto>(),
                It.IsAny<decimal>(), It.IsAny<decimal>(), It.IsAny<CancellationToken>()))
            .ReturnsAsync(new Backend.Models.DTOs.PolygonResponses.PortfolioScenarioResponseDto
            {
                Symbol = "SPY",
                SpotPrice = 500m,
                Points =
                [
                    new Backend.Models.DTOs.PolygonResponses.ScenarioPointDto
                    {
                        SpotShock = -0.10m,
                        Spot = 450m,
                        Legs =
                        [
                            new Backend.Models.DTOs.PolygonResponses.LegGreeksDto
                            {
                                LegId = positionLegId,
                                Instrument = "stock",
                                TheoreticalPrice = 450m,
                                Delta = 1m,
                            }
                        ],
                    }
                ],
            });

        var prices = new Dictionary<string, decimal> { ["SPY"] = 500m };
        var scenario = new ScenarioInput { PriceChangePercent = -0.10m };

        var result = await service.RunScenarioAsync(account.Id, prices, scenario);

        // Stock leg: 100 shares × 450 × multiplier 1 = 45,000.
        // Cash 50,000 + scenario market value 45,000 = 95,000 equity.
        Assert.Equal(100_000m, result.CurrentEquity);
        Assert.Equal(95_000m, result.ScenarioEquity);
        Assert.Equal(-5_000m, result.PnLImpact);
    }

    [Fact]
    public async Task RunScenario_NoMatchingPriceForUnderlying_SkipsPosition()
    {
        var (service, context, valuationMock, polygonMock) = CreateServiceWithContext();
        var (account, ticker) = SeedAccountAndTicker(context, 50_000m);
        CreateOpenPosition(context, account.Id, ticker.Id, 100, 500m, AssetType.Stock, null);

        valuationMock.Setup(v => v.ComputeValuationWithPricesAsync(
                account.Id, It.IsAny<Dictionary<string, decimal>>(), It.IsAny<CancellationToken>()))
            .ReturnsAsync(new PortfolioValuation { Cash = 50_000m, Equity = 50_000m });

        // Empty prices dictionary → no underlying spot known → Python is never called.
        var prices = new Dictionary<string, decimal>();
        var scenario = new ScenarioInput { PriceChangePercent = -0.10m };

        var result = await service.RunScenarioAsync(account.Id, prices, scenario);

        polygonMock.Verify(p => p.PortfolioScenarioAsync(
            It.IsAny<long>(), It.IsAny<decimal>(),
            It.IsAny<List<Backend.Models.DTOs.PolygonResponses.PortfolioScenarioPositionDto>>(),
            It.IsAny<Backend.Models.DTOs.PolygonResponses.PortfolioScenarioGridDto>(),
            It.IsAny<decimal>(), It.IsAny<decimal>(), It.IsAny<CancellationToken>()),
            Times.Never);

        Assert.Empty(result.Positions);
        Assert.Equal(50_000m, result.ScenarioEquity); // cash only
    }

    #endregion

    #region PortfolioVega

    [Fact]
    public async Task ComputePortfolioVega_AggregatesLiveGreeksAcrossPositions()
    {
        var (service, context, _, polygonMock) = CreateServiceWithContext();
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

        var position = CreateOpenPosition(context, account.Id, ticker.Id, 10, 5m,
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
            EntryVega = 0.05m, // intentionally different from the live-Greeks mock
            EntryDelta = 0.5m,
            EntryIV = 0.20m,
        };
        context.OptionLegs.Add(leg);
        context.SaveChanges();

        // Resolver doesn't take prices; the service calls FetchStockSnapshotsAsync
        // for the underlying spot, then PortfolioLiveGreeksAsync for the recomputed Greeks.
        polygonMock
            .Setup(p => p.FetchStockSnapshotsAsync(
                It.Is<List<string>>(list => list.Contains("SPY")), It.IsAny<CancellationToken>()))
            .ReturnsAsync(new Backend.Models.DTOs.PolygonResponses.StockSnapshotsResponse
            {
                Success = true,
                Snapshots =
                [
                    new Backend.Models.DTOs.PolygonResponses.StockTickerSnapshotDto
                    {
                        Ticker = "SPY",
                        Min = new Backend.Models.DTOs.PolygonResponses.MinuteBarDto { Close = 505m },
                    }
                ],
            });

        polygonMock
            .Setup(p => p.PortfolioLiveGreeksAsync(
                It.IsAny<long>(), It.IsAny<decimal>(),
                It.Is<List<Backend.Models.DTOs.PolygonResponses.PortfolioScenarioPositionDto>>(
                    list => list.Count == 1 && list[0].LegId == position.Id.ToString()),
                It.IsAny<decimal>(), It.IsAny<decimal>(), It.IsAny<CancellationToken>()))
            .ReturnsAsync(new Backend.Models.DTOs.PolygonResponses.PortfolioScenarioResponseDto
            {
                Symbol = "SPY",
                SpotPrice = 505m,
                Points =
                [
                    new Backend.Models.DTOs.PolygonResponses.ScenarioPointDto
                    {
                        Spot = 505m,
                        Legs =
                        [
                            new Backend.Models.DTOs.PolygonResponses.LegGreeksDto
                            {
                                LegId = position.Id.ToString(),
                                Instrument = "option",
                                Vega = 0.15m,
                            }
                        ],
                    }
                ],
            });

        var vega = await service.ComputePortfolioVegaAsync(account.Id);

        // totalVega = 0.15 * 10 * 100 = 150
        Assert.Equal(150m, vega);
    }

    #endregion
}

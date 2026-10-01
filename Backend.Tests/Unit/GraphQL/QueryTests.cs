using Backend.GraphQL;
using Backend.Models.DTOs.PolygonResponses;
using Backend.Models.MarketData;
using Backend.Services.Interfaces;
using Backend.Tests.Helpers;
using Microsoft.Extensions.Logging;
using Moq;

namespace Backend.Tests.Unit.GraphQL;

public class QueryTests
{
    private readonly Mock<IMarketDataService> _marketDataMock = new();
    private readonly Mock<IPolygonService> _polygonMock = new();
    private readonly Mock<ILogger<Query>> _loggerMock = new();

    #region GetOrFetchStockAggregates

    [Fact]
    public async Task GetOrFetchStockAggregates_WithData_MapsBarsToUnixMs()
    {
        var context = TestDbContextFactory.Create();
        var ticker = new Ticker { Symbol = "AAPL", Name = "AAPL", Market = "stocks" };
        context.Tickers.Add(ticker);
        await context.SaveChangesAsync();

        var aggregates = new List<StockAggregate>
        {
            new() { TickerId = ticker.Id, Open = 150m, High = 160m, Low = 145m, Close = 155m, Volume = 1_000_000m, Timestamp = new DateTime(2026, 1, 15, 0, 0, 0, DateTimeKind.Utc), Timespan = "day", Multiplier = 1 },
            new() { TickerId = ticker.Id, Open = 155m, High = 165m, Low = 150m, Close = 162m, Volume = 900_000m, Timestamp = new DateTime(2026, 1, 16, 0, 0, 0, DateTimeKind.Utc), Timespan = "day", Multiplier = 1 },
        };

        _marketDataMock.Setup(s => s.GetOrFetchAggregatesAsync(
                "AAPL", 1, "day", "2026-01-01", "2026-01-31", false, true, default))
            .ReturnsAsync(aggregates);

        var query = new Query();
        var result = await query.GetOrFetchStockAggregates(
            _marketDataMock.Object, _loggerMock.Object, context,
            "AAPL", "2026-01-01", "2026-01-31");

        Assert.Equal("AAPL", result.Ticker);
        Assert.Equal(2, result.Aggregates.Count);
        Assert.Equal(1_768_435_200_000L, result.Aggregates[0].Timestamp);
        Assert.Equal(165m, result.Aggregates[1].High);
    }

    #endregion

    #region GetOptionsChainSnapshot

    [Fact]
    public async Task GetOptionsChainSnapshot_Success_MapsFullResponse()
    {
        _polygonMock.Setup(s => s.FetchOptionsChainSnapshotAsync("AAPL", "2026-02-20", default))
            .ReturnsAsync(new OptionsChainSnapshotResponse
            {
                Success = true,
                Underlying = new UnderlyingSnapshotDto { Ticker = "AAPL", Price = 230m, Change = 2.5m, ChangePercent = 1.1m },
                Contracts = [new OptionsContractSnapshotDto
                {
                    Ticker = "O:AAPL260220C00230000",
                    ContractType = "call",
                    StrikePrice = 230m,
                    ImpliedVolatility = 0.35m,
                    Greeks = new GreeksSnapshotDto { Delta = 0.5m, Gamma = 0.02m, Theta = -0.05m, Vega = 0.15m },
                    Day = new DaySnapshotDto { Open = 5m, High = 6m, Low = 4.5m, Close = 5.5m, Volume = 500m }
                }],
                Count = 1
            });

        var query = new Query();
        var result = await query.GetOptionsChainSnapshot(
            _polygonMock.Object, _loggerMock.Object, "AAPL", "2026-02-20");

        Assert.True(result.Success);
        Assert.NotNull(result.Underlying);
        Assert.Equal(230m, result.Underlying!.Price);
        Assert.Single(result.Contracts);
        Assert.Equal(0.5m, result.Contracts[0].Greeks!.Delta);
        Assert.Equal(5m, result.Contracts[0].Day!.Open);
    }

    [Fact]
    public async Task GetOptionsChainSnapshot_NullUnderlying_HandledGracefully()
    {
        _polygonMock.Setup(s => s.FetchOptionsChainSnapshotAsync("AAPL", null, default))
            .ReturnsAsync(new OptionsChainSnapshotResponse
            {
                Success = true,
                Underlying = null,
                Contracts = [],
                Count = 0
            });

        var query = new Query();
        var result = await query.GetOptionsChainSnapshot(
            _polygonMock.Object, _loggerMock.Object, "AAPL");

        Assert.True(result.Success);
        Assert.Null(result.Underlying);
        Assert.Empty(result.Contracts);
    }

    #endregion

    #region GetOptionsExpirations

    [Fact]
    public async Task GetOptionsExpirations_Success_MapsExpirationList()
    {
        _polygonMock.Setup(s => s.FetchOptionsExpirationsAsync(
                "AAPL", null, null, null, default))
            .ReturnsAsync(new OptionsExpirationsResponse
            {
                Success = true,
                Expirations = ["2026-02-20", "2026-03-20", "2026-04-17"],
                Count = 3,
            });

        var query = new Query();
        var result = await query.GetOptionsExpirations(
            _polygonMock.Object, _loggerMock.Object, "AAPL");

        Assert.True(result.Success);
        Assert.Equal(3, result.Count);
        Assert.Equal(3, result.Expirations.Count);
        Assert.Equal("2026-02-20", result.Expirations[0]);
    }

    #endregion

    #region AnalyzeOptionsStrategy

    [Fact]
    public async Task AnalyzeOptionsStrategy_Success_MapsBaseShape()
    {
        var legs = new List<StrategyLegInput>
        {
            new() { Strike = 230m, OptionType = "call", Position = "long", Premium = 5m, Iv = 0.30m, Quantity = 1 },
            new() { Strike = 235m, OptionType = "call", Position = "short", Premium = 3m, Iv = 0.28m, Quantity = 1 },
        };

        _polygonMock.Setup(s => s.AnalyzeOptionsStrategyAsync(
                "AAPL", legs, "2026-02-20", 230m, null,
                It.IsAny<StrategyAnalyzeOptions>(), default))
            .ReturnsAsync(new StrategyAnalyzeResponseDto
            {
                Success = true,
                Symbol = "AAPL",
                SpotPrice = 230m,
                StrategyCost = 2m,
                Pop = 0.45m,
                ExpectedValue = 0.5m,
                MaxProfit = 3m,
                MaxLoss = -2m,
                Breakevens = [232m],
                Curve =
                [
                    new PayoffPointDto { Price = 220m, Pnl = -2m },
                    new PayoffPointDto { Price = 230m, Pnl = -2m },
                    new PayoffPointDto { Price = 240m, Pnl = 3m },
                ],
                Greeks = new GreeksDto { Delta = 0.05m, Gamma = 0.01m, Theta = -0.02m, Vega = 0.05m },
            });

        var query = new Query();
        var result = await query.AnalyzeOptionsStrategy(
            _polygonMock.Object, _loggerMock.Object,
            "AAPL", legs, "2026-02-20", 230m);

        Assert.True(result.Success);
        Assert.Equal("AAPL", result.Symbol);
        Assert.Equal(2m, result.StrategyCost);
        Assert.Equal(0.45m, result.Pop);
        Assert.Equal(3, result.Curve.Count);
        Assert.Equal(0.05m, result.Greeks!.Delta);
        Assert.Single(result.Breakevens);
        Assert.Null(result.CurrentCurve);
        Assert.Null(result.GreekCurves);
        Assert.Null(result.LegDiagnostics);
    }

    [Fact]
    public async Task AnalyzeOptionsStrategy_Phase11Flags_PropagateToService()
    {
        var legs = new List<StrategyLegInput>
        {
            new() { Strike = 100m, OptionType = "call", Position = "long", Premium = 5m, Iv = 0.30m, Quantity = 1 },
        };

        StrategyAnalyzeOptions? capturedOptions = null;
        _polygonMock.Setup(s => s.AnalyzeOptionsStrategyAsync(
                It.IsAny<string>(), It.IsAny<List<StrategyLegInput>>(),
                It.IsAny<string>(), It.IsAny<decimal>(), It.IsAny<decimal?>(),
                It.IsAny<StrategyAnalyzeOptions>(), default))
            .Callback<string, List<StrategyLegInput>, string, decimal, decimal?,
                      StrategyAnalyzeOptions?, CancellationToken>(
                (_, _, _, _, _, opts, _) => capturedOptions = opts)
            .ReturnsAsync(new StrategyAnalyzeResponseDto
            {
                Success = true,
                Symbol = "AAPL",
                CurrentCurve = [new CurrentCurvePointDto { Price = 100m, TheoreticalValue = 5m, TheoreticalPnl = 0m }],
                GreekCurves = [new GreekCurvePointDto { Price = 100m, Delta = 0.5m, Gamma = 0.02m, Theta = -0.05m, Vega = 0.15m }],
                LegDiagnostics = [new LegDiagnosticDto { LegId = "leg1", Strike = 100m, OptionType = "call", Position = "long", Quantity = 1, Iv = 0.30m, EntryPremium = 5m, CurrentTheoretical = 5m, CurrentDelta = 0.5m, CurrentGamma = 0.02m, CurrentTheta = -0.05m, CurrentVega = 0.15m, LegPnl = 0m }],
                Greeks = new GreeksDto(),
            });

        var query = new Query();
        var result = await query.AnalyzeOptionsStrategy(
            _polygonMock.Object, _loggerMock.Object,
            "AAPL", legs, "2026-02-20", 100m,
            includeCurrentCurve: true,
            includeGreekCurves: true,
            includeLegDiagnostics: true,
            whatIfTimeShiftDays: 5m,
            whatIfIvShift: 0.10m);

        Assert.True(result.Success);
        Assert.NotNull(capturedOptions);
        Assert.True(capturedOptions!.IncludeCurrentCurve);
        Assert.True(capturedOptions.IncludeGreekCurves);
        Assert.True(capturedOptions.IncludeLegDiagnostics);
        Assert.Equal(5m, capturedOptions.WhatIfTimeShiftDays);
        Assert.Equal(0.10m, capturedOptions.WhatIfIvShift);
        Assert.NotNull(result.CurrentCurve);
        Assert.NotNull(result.GreekCurves);
        Assert.NotNull(result.LegDiagnostics);
        Assert.Single(result.LegDiagnostics!);
        Assert.Equal(0.5m, result.LegDiagnostics![0].CurrentDelta);
    }

    #endregion

    #region PricingModelComparison

    [Fact]
    public async Task PricingModelComparison_Success_MapsModelCurves()
    {
        _polygonMock.Setup(s => s.PricingCompareAsync(
                100m, 100m, 0.20m, "2026-02-20", "call",
                null, 0m, null, null, null, 100, default))
            .ReturnsAsync(new PricingCompareResponse
            {
                Success = true,
                Strike = 100m,
                OptionType = "call",
                ExpirationDate = "2026-02-20",
                TimeToExpiryYears = 0.0822m, // ~30 days / 365
                RiskFreeRate = 0.043m,
                Models =
                [
                    new PricingModelCurveDto
                    {
                        Model = "python_bs",
                        Points =
                        [
                            new PricingPointDto { Spot = 95m, Price = 1.10m, Delta = 0.40m, Gamma = 0.04m, Theta = -0.03m, Vega = 0.10m, Rho = 0.04m },
                            new PricingPointDto { Spot = 100m, Price = 2.50m, Delta = 0.55m, Gamma = 0.045m, Theta = -0.035m, Vega = 0.12m, Rho = 0.05m },
                        ],
                    },
                    new PricingModelCurveDto
                    {
                        Model = "quantlib_bs",
                        Points =
                        [
                            new PricingPointDto { Spot = 95m, Price = 1.099m, Delta = 0.40m, Gamma = 0.04m, Theta = -0.03m, Vega = 0.10m, Rho = 0.04m },
                            new PricingPointDto { Spot = 100m, Price = 2.499m, Delta = 0.55m, Gamma = 0.045m, Theta = -0.035m, Vega = 0.12m, Rho = 0.05m },
                        ],
                    },
                ],
            });

        var query = new Query();
        var result = await query.PricingModelComparison(
            _polygonMock.Object, _loggerMock.Object,
            spot: 100m, strike: 100m, volatility: 0.20m,
            expirationDate: "2026-02-20", optionType: "call");

        Assert.True(result.Success);
        Assert.Equal(100m, result.Strike);
        Assert.Equal("call", result.OptionType);
        Assert.Equal(0.0822m, result.TimeToExpiryYears);
        Assert.Equal(0.043m, result.RiskFreeRate);
        Assert.Equal(2, result.Models.Count);
        Assert.Equal("python_bs", result.Models[0].Model);
        Assert.Equal(2, result.Models[0].Points.Count);
        Assert.Equal(2.50m, result.Models[0].Points[1].Price);
        Assert.Equal("quantlib_bs", result.Models[1].Model);
    }

    #endregion
}

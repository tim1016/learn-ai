using Backend.Data;
using Backend.GraphQL.Comparison;
using Backend.Models.Comparison;
using Backend.Models.MarketData;
using Backend.Services.Interfaces;
using Backend.Tests.Helpers;
using Moq;

namespace Backend.Tests.Unit.GraphQL;

public class CompareBacktestRunsTests : IDisposable
{
    private readonly AppDbContext _db;
    private readonly Mock<IComparisonService> _comparisonMock;

    public CompareBacktestRunsTests()
    {
        _db = TestDbContextFactory.Create();
        _comparisonMock = new Mock<IComparisonService>();
    }

    public void Dispose() => _db.Dispose();

    private async Task<int> SeedRunAsync(
        string source,
        string strategyName,
        string parameters,
        string startDate,
        string endDate,
        decimal pnl,
        int tradeCount,
        decimal fees,
        double winRate,
        decimal finalEquity)
    {
        var ticker = _db.Tickers.FirstOrDefault(t => t.Symbol == "SPY");

        if (ticker is null)
        {
            ticker = new Ticker { Symbol = "SPY", Name = "SPDR S&P 500", Market = "stocks" };
            _db.Tickers.Add(ticker);
            await _db.SaveChangesAsync();
        }

        var ex = new StrategyExecution
        {
            TickerId = ticker.Id,
            StrategyName = strategyName,
            Parameters = parameters,
            StartDate = startDate,
            EndDate = endDate,
            Timespan = "minute",
            Multiplier = 15,
            TotalTrades = tradeCount,
            WinningTrades = 0,
            LosingTrades = 0,
            TotalPnL = pnl,
            InitialCash = 100_000m,
            FinalEquity = finalEquity,
            TotalFees = fees,
            WinRate = (decimal)winRate,
            Source = source,
            ExecutedAt = DateTime.UtcNow,
            DurationMs = 0,
        };
        _db.StrategyExecutions.Add(ex);
        await _db.SaveChangesAsync();
        return ex.Id;
    }

    private void SetupComparisonService(
        IReadOnlyList<TradeDivergenceRecord>? divergences = null,
        long? firstDivergenceMsUtc = null)
    {
        _comparisonMock
            .Setup(s => s.CompareTradesAsync(It.IsAny<CompareTradesRequest>(), It.IsAny<CancellationToken>()))
            .ReturnsAsync(new CompareTradesResponse(
                Divergences: divergences ?? Array.Empty<TradeDivergenceRecord>(),
                FirstDivergenceMsUtc: firstDivergenceMsUtc));
    }

    [Fact]
    public async Task GetCompareBacktestRunsAsync_ReturnsCombinedResult()
    {
        var leftId = await SeedRunAsync(
            "lean-sidecar", "ema_crossover", "{\"symbol\":\"SPY\"}",
            "2025-01-06", "2025-01-10", 9m, 1, 1m, 1.0, 100_008m);
        var rightId = await SeedRunAsync(
            "engine", "ema_crossover", "{\"symbol\":\"SPY\"}",
            "2025-01-06", "2025-01-10", 9m, 1, 1m, 1.0, 100_008m);
        SetupComparisonService();

        var result = await CompareBacktestRunsResolver.GetCompareBacktestRunsAsync(
            leftId, rightId, _db, _comparisonMock.Object, CancellationToken.None);

        Assert.NotNull(result);
        Assert.Equal(leftId, result.Left.Id);
        Assert.Equal(rightId, result.Right.Id);
        Assert.True(result.Guardrails.SameAlgorithm);
        Assert.True(result.Guardrails.SameSymbol);
        Assert.True(result.Guardrails.SameWindow);
        Assert.Equal(0m, result.Summary.PnlDelta);
        Assert.Empty(result.Divergences);
    }

    [Fact]
    public async Task GetCompareBacktestRunsAsync_FlagsSymbolMismatch()
    {
        var leftId = await SeedRunAsync(
            "lean-sidecar", "ema_crossover", "{\"symbol\":\"SPY\"}",
            "2025-01-06", "2025-01-10", 9m, 1, 1m, 1.0, 100_008m);
        var rightId = await SeedRunAsync(
            "engine", "ema_crossover", "{\"symbol\":\"QQQ\"}",
            "2025-01-06", "2025-01-10", 9m, 1, 1m, 1.0, 100_008m);
        SetupComparisonService();

        var result = await CompareBacktestRunsResolver.GetCompareBacktestRunsAsync(
            leftId, rightId, _db, _comparisonMock.Object, CancellationToken.None);

        Assert.NotNull(result);
        Assert.False(result.Guardrails.SameSymbol);
        Assert.Contains(result.Guardrails.Warnings, w => w.Contains("symbol"));
    }

    [Fact]
    public async Task GetCompareBacktestRunsAsync_ComputesSummaryDeltas()
    {
        var leftId = await SeedRunAsync(
            "lean-sidecar", "ema_crossover", "{\"symbol\":\"SPY\"}",
            "2025-01-06", "2025-01-10", 9m, 1, 1m, 1.0, 100_008m);
        var rightId = await SeedRunAsync(
            "engine", "ema_crossover", "{\"symbol\":\"SPY\"}",
            "2025-01-06", "2025-01-10", 12m, 2, 2m, 0.5, 100_010m);
        SetupComparisonService();

        var result = await CompareBacktestRunsResolver.GetCompareBacktestRunsAsync(
            leftId, rightId, _db, _comparisonMock.Object, CancellationToken.None);

        Assert.NotNull(result);
        Assert.Equal(3m, result.Summary.PnlDelta);               // right - left
        Assert.Equal(1, result.Summary.TradeCountDelta);
        Assert.Equal(2m, result.Summary.FinalEquityDelta);
        Assert.True(Math.Abs(result.Summary.WinRateDelta - (-0.5)) < 0.001);
    }

    [Fact]
    public async Task GetCompareBacktestRunsAsync_PassesTradesToPythonService()
    {
        var leftId = await SeedRunAsync(
            "lean-sidecar", "ema_crossover", "{\"symbol\":\"SPY\"}",
            "2025-01-06", "2025-01-10", 9m, 1, 1m, 1.0, 100_008m);
        var rightId = await SeedRunAsync(
            "engine", "ema_crossover", "{\"symbol\":\"SPY\"}",
            "2025-01-06", "2025-01-10", 9m, 1, 1m, 1.0, 100_008m);

        _db.BacktestTrades.Add(new BacktestTrade
        {
            StrategyExecutionId = leftId,
            TradeType = "LONG",
            EntryTimestamp = new DateTime(2025, 1, 6, 14, 30, 0, DateTimeKind.Utc),
            ExitTimestamp = new DateTime(2025, 1, 6, 14, 45, 0, DateTimeKind.Utc),
            EntryPrice = 100m,
            ExitPrice = 101m,
            Quantity = 10m,
            PnL = 10m,
            CumulativePnL = 10m,
            SignalReason = "test",
            IsSyntheticExit = false,
        });
        _db.BacktestTrades.Add(new BacktestTrade
        {
            StrategyExecutionId = rightId,
            TradeType = "LONG",
            EntryTimestamp = new DateTime(2025, 1, 6, 14, 30, 0, DateTimeKind.Utc),
            ExitTimestamp = new DateTime(2025, 1, 6, 14, 45, 0, DateTimeKind.Utc),
            EntryPrice = 100m,
            ExitPrice = 101m,
            Quantity = 10m,
            PnL = 10m,
            CumulativePnL = 10m,
            SignalReason = "test",
            IsSyntheticExit = false,
        });
        await _db.SaveChangesAsync();
        SetupComparisonService();

        await CompareBacktestRunsResolver.GetCompareBacktestRunsAsync(
            leftId, rightId, _db, _comparisonMock.Object, CancellationToken.None);

        _comparisonMock.Verify(s => s.CompareTradesAsync(
            It.Is<CompareTradesRequest>(r => r.LeftTrades.Count == 1 && r.RightTrades.Count == 1),
            It.IsAny<CancellationToken>()), Times.Once);
    }

    [Fact]
    public async Task GetCompareBacktestRunsAsync_ReturnsNullWhenLeftMissing()
    {
        var rightId = await SeedRunAsync(
            "engine", "ema_crossover", "{\"symbol\":\"SPY\"}",
            "2025-01-06", "2025-01-10", 9m, 1, 1m, 1.0, 100_008m);

        var result = await CompareBacktestRunsResolver.GetCompareBacktestRunsAsync(
            9999, rightId, _db, _comparisonMock.Object, CancellationToken.None);

        Assert.Null(result);
    }

    [Fact]
    public async Task GetCompareBacktestRunsAsync_MapsDivergencesFromPythonResponse()
    {
        var leftId = await SeedRunAsync(
            "lean-sidecar", "ema_crossover", "{\"symbol\":\"SPY\"}",
            "2025-01-06", "2025-01-10", 9m, 1, 1m, 1.0, 100_008m);
        var rightId = await SeedRunAsync(
            "engine", "ema_crossover", "{\"symbol\":\"SPY\"}",
            "2025-01-06", "2025-01-10", 9m, 1, 1m, 1.0, 100_008m);
        SetupComparisonService(
            divergences: new[]
            {
                new TradeDivergenceRecord(
                    Category: "FILL_PRICE_DRIFT",
                    TradeNumber: 1,
                    MsUtc: 1_700_000_000_000,
                    Message: "drift",
                    LeftFillPrice: 100m,
                    RightFillPrice: 100.10m),
            },
            firstDivergenceMsUtc: 1_700_000_000_000);

        var result = await CompareBacktestRunsResolver.GetCompareBacktestRunsAsync(
            leftId, rightId, _db, _comparisonMock.Object, CancellationToken.None);

        Assert.NotNull(result);
        Assert.Single(result.Divergences);
        Assert.Equal(DivergenceCategory.FILL_PRICE_DRIFT, result.Divergences[0].Category);
        Assert.Equal(100m, result.Divergences[0].LeftFillPrice);
        Assert.Equal(1_700_000_000_000, result.FirstDivergenceMsUtc);
    }
}

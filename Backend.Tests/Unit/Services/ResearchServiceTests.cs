using System.Text.Json;
using Backend.Models.DTOs;
using Backend.Models.MarketData;
using Backend.Services.Implementation;
using Backend.Tests.Helpers;

namespace Backend.Tests.Unit.Services;

public class ResearchServiceTests
{
    private static readonly JsonSerializerOptions _jsonOptions = new()
    {
        PropertyNamingPolicy = JsonNamingPolicy.SnakeCaseLower
    };

    #region GetExperimentsAsync

    [Fact]
    public async Task GetExperimentsAsync_WithData_ReturnsOrderedHistory()
    {
        // Arrange
        var context = TestDbContextFactory.Create();

        var ticker = new Ticker { Id = 1, Symbol = "AAPL", Name = "Apple Inc.", Market = "stocks" };
        context.Tickers.Add(ticker);

        context.ResearchExperiments.AddRange(
            new ResearchExperiment
            {
                TickerId = 1,
                FeatureName = "momentum_5m",
                StartDate = "2024-01-01",
                EndDate = "2024-01-31",
                BarsUsed = 200,
                MeanIC = 0.15m,
                ICTStat = 2.5m,
                ICPValue = 0.02m,
                AdfPValue = 0.001m,
                KpssPValue = 0.3m,
                IsStationary = true,
                PassedValidation = true,
                MonotonicityRatio = 1.0m,
                IsMonotonic = true,
                CreatedAt = DateTime.UtcNow.AddDays(-1),
            },
            new ResearchExperiment
            {
                TickerId = 1,
                FeatureName = "rsi_14",
                StartDate = "2024-01-01",
                EndDate = "2024-01-31",
                BarsUsed = 200,
                MeanIC = 0.08m,
                ICTStat = 1.2m,
                ICPValue = 0.15m,
                AdfPValue = 0.01m,
                KpssPValue = 0.4m,
                IsStationary = true,
                PassedValidation = false,
                MonotonicityRatio = 0.5m,
                IsMonotonic = false,
                CreatedAt = DateTime.UtcNow,
            }
        );
        await context.SaveChangesAsync();

        var service = new ResearchService(context);

        // Act
        var result = await service.GetExperimentsAsync("AAPL");

        // Assert
        Assert.Equal(2, result.Count);
        Assert.Equal("rsi_14", result[0].FeatureName);
        Assert.Equal("momentum_5m", result[1].FeatureName);
    }

    #endregion

    #region GetSignalExperimentsAsync

    [Fact]
    public async Task GetSignalExperimentsAsync_WithData_ReturnsOrderedHistory()
    {
        // Arrange
        var context = TestDbContextFactory.Create();

        var ticker = new Ticker { Id = 1, Symbol = "AAPL", Name = "Apple Inc.", Market = "stocks" };
        context.Tickers.Add(ticker);

        context.SignalExperiments.AddRange(
            new SignalExperiment
            {
                TickerId = 1,
                FeatureName = "momentum_5m",
                StartDate = "2024-01-01",
                EndDate = "2024-01-31",
                BarsUsed = 200,
                OverallGrade = "B",
                StatusLabel = "Candidate",
                OverallPassed = true,
                MeanOosSharpe = 0.8m,
                BestThreshold = 1.0m,
                BestCostBps = 5m,
                FlipSign = true,
                RegimeGateEnabled = true,
                CreatedAt = DateTime.UtcNow.AddDays(-1),
            },
            new SignalExperiment
            {
                TickerId = 1,
                FeatureName = "rsi_14",
                StartDate = "2024-01-01",
                EndDate = "2024-01-31",
                BarsUsed = 200,
                OverallGrade = "D",
                StatusLabel = "Exploratory",
                OverallPassed = false,
                MeanOosSharpe = -0.2m,
                BestThreshold = 0.5m,
                BestCostBps = 10m,
                FlipSign = false,
                RegimeGateEnabled = true,
                CreatedAt = DateTime.UtcNow,
            }
        );
        await context.SaveChangesAsync();

        var service = new ResearchService(context);

        // Act
        var result = await service.GetSignalExperimentsAsync("AAPL");

        // Assert
        Assert.Equal(2, result.Count);
        Assert.Equal("rsi_14", result[0].FeatureName); // most recent first
        Assert.Equal("momentum_5m", result[1].FeatureName);
    }

    #endregion

    #region GetSignalExperimentReportAsync

    private static SignalEngineReportDto CreateSuccessSignalReport()
    {
        return new SignalEngineReportDto
        {
            Success = true,
            Ticker = "AAPL",
            FeatureName = "momentum_5m",
            StartDate = "2024-01-01",
            EndDate = "2024-01-31",
            BarsUsed = 200,
            FlipSign = true,
            ThresholdsTested = [0.5, 1.0, 1.5],
            CostBpsOptions = [5, 10],
            BestThreshold = 1.0,
            BestCostBps = 5,
            BacktestGrid = [],
            WalkForward = new WalkForwardResultDto { MeanOosSharpe = 0.8 },
            Graduation = new GraduationResultDto
            {
                OverallPassed = true,
                OverallGrade = "B",
                Summary = "Passed",
                StatusLabel = "Candidate",
            },
            Methodology = new MethodologyDto { RegimeGateEnabled = true },
        };
    }

    [Fact]
    public async Task GetSignalExperimentReportAsync_Found_DeserializesReport()
    {
        // Arrange
        var context = TestDbContextFactory.Create();

        var ticker = new Ticker { Id = 1, Symbol = "AAPL", Name = "Apple Inc.", Market = "stocks" };
        context.Tickers.Add(ticker);

        var report = CreateSuccessSignalReport();
        var experiment = new SignalExperiment
        {
            TickerId = 1,
            FeatureName = "momentum_5m",
            StartDate = "2024-01-01",
            EndDate = "2024-01-31",
            BarsUsed = 200,
            OverallGrade = "B",
            StatusLabel = "Candidate",
            OverallPassed = true,
            MeanOosSharpe = 0.8m,
            BestThreshold = 1.0m,
            BestCostBps = 5m,
            FlipSign = true,
            RegimeGateEnabled = true,
            JsonReport = JsonSerializer.Serialize(report, _jsonOptions),
        };
        context.SignalExperiments.Add(experiment);
        await context.SaveChangesAsync();

        var service = new ResearchService(context);

        // Act
        var result = await service.GetSignalExperimentReportAsync(experiment.Id);

        // Assert
        Assert.NotNull(result);
        Assert.True(result!.Success);
        Assert.Equal("AAPL", result.Ticker);
        Assert.Equal("momentum_5m", result.FeatureName);
        Assert.Equal(1.0, result.BestThreshold);
    }

    #endregion
}

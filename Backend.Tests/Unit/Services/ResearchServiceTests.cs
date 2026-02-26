using System.Net;
using System.Text.Json;
using Backend.Data;
using Backend.Models.DTOs;
using Backend.Models.MarketData;
using Backend.Services.Implementation;
using Backend.Services.Interfaces;
using Backend.Tests.Helpers;
using Microsoft.Extensions.Logging;
using Moq;

namespace Backend.Tests.Unit.Services;

public class ResearchServiceTests
{
    private readonly Mock<IMarketDataService> _marketDataServiceMock = new();
    private readonly Mock<ILogger<ResearchService>> _loggerMock = new();

    private static readonly JsonSerializerOptions _jsonOptions = new()
    {
        PropertyNamingPolicy = JsonNamingPolicy.SnakeCaseLower
    };

    #region Helpers

    private ResearchService CreateService(HttpClient httpClient, AppDbContext? context = null)
    {
        context ??= TestDbContextFactory.Create();
        return new ResearchService(
            httpClient, _loggerMock.Object, context, _marketDataServiceMock.Object);
    }

    private static HttpClient CreateMockHttpClient(ResearchReportDto report, HttpStatusCode statusCode = HttpStatusCode.OK)
    {
        var handler = new FakeHttpMessageHandler(
            JsonSerializer.Serialize(report, _jsonOptions), statusCode);
        return new HttpClient(handler) { BaseAddress = new Uri("http://localhost:8000") };
    }

    private static List<StockAggregate> CreateSampleAggregates(int count = 200)
    {
        var aggregates = new List<StockAggregate>();
        var baseTs = 1704117000000L;

        for (var i = 0; i < count; i++)
        {
            aggregates.Add(new StockAggregate
            {
                Id = i + 1,
                TickerId = 1,
                Timestamp = baseTs + i * 60_000,
                Open = 100.0m + i * 0.01m,
                High = 101.0m + i * 0.01m,
                Low = 99.0m + i * 0.01m,
                Close = 100.5m + i * 0.01m,
                Volume = 1_000_000m,
            });
        }

        return aggregates;
    }

    private static ResearchReportDto CreateSuccessReport()
    {
        return new ResearchReportDto
        {
            Success = true,
            Ticker = "AAPL",
            FeatureName = "momentum_5m",
            StartDate = "2024-01-01",
            EndDate = "2024-01-31",
            BarsUsed = 200,
            MeanIc = 0.15,
            IcTStat = 2.5,
            IcPValue = 0.02,
            IcValues = [0.12, 0.18, 0.14],
            IcDates = ["2024-01-01", "2024-01-02", "2024-01-03"],
            AdfPvalue = 0.001,
            KpssPvalue = 0.3,
            IsStationary = true,
            QuantileBins =
            [
                new QuantileBinDto { BinNumber = 1, LowerBound = -0.02, UpperBound = -0.005, MeanReturn = -0.008, Count = 40 },
                new QuantileBinDto { BinNumber = 2, LowerBound = -0.005, UpperBound = 0.0, MeanReturn = -0.002, Count = 40 },
                new QuantileBinDto { BinNumber = 3, LowerBound = 0.0, UpperBound = 0.005, MeanReturn = 0.001, Count = 40 },
                new QuantileBinDto { BinNumber = 4, LowerBound = 0.005, UpperBound = 0.01, MeanReturn = 0.004, Count = 40 },
                new QuantileBinDto { BinNumber = 5, LowerBound = 0.01, UpperBound = 0.02, MeanReturn = 0.009, Count = 40 },
            ],
            IsMonotonic = true,
            MonotonicityRatio = 1.0,
            PassedValidation = true,
        };
    }

    #endregion

    #region RunFeatureResearchAsync

    [Fact]
    public async Task RunFeatureResearchAsync_ValidRequest_ReturnsSuccessResult()
    {
        // Arrange
        var aggregates = CreateSampleAggregates();
        _marketDataServiceMock
            .Setup(s => s.GetOrFetchAggregatesAsync(
                "AAPL", 1, "minute", "2024-01-01", "2024-01-31", false, default))
            .ReturnsAsync(aggregates);

        var report = CreateSuccessReport();
        var httpClient = CreateMockHttpClient(report);

        var context = TestDbContextFactory.Create();
        var ticker = new Ticker { Id = 1, Symbol = "AAPL", Name = "Apple Inc.", Market = "stocks" };
        context.Tickers.Add(ticker);
        await context.SaveChangesAsync();

        _marketDataServiceMock
            .Setup(s => s.GetOrCreateTickerAsync("AAPL", "stocks", default))
            .ReturnsAsync(ticker);

        var service = CreateService(httpClient, context);

        // Act
        var result = await service.RunFeatureResearchAsync(
            "AAPL", "momentum_5m", "2024-01-01", "2024-01-31");

        // Assert
        Assert.True(result.Success);
        Assert.Equal("AAPL", result.Ticker);
        Assert.Equal("momentum_5m", result.FeatureName);
        Assert.Equal(0.15, result.MeanIc);
        Assert.True(result.PassedValidation);
        Assert.Equal(5, result.QuantileBins.Count);
    }

    [Fact]
    public async Task RunFeatureResearchAsync_NoAggregates_ReturnsError()
    {
        // Arrange
        _marketDataServiceMock
            .Setup(s => s.GetOrFetchAggregatesAsync(
                "AAPL", 1, "minute", "2024-01-01", "2024-01-31", false, default))
            .ReturnsAsync([]);

        var httpClient = CreateMockHttpClient(new ResearchReportDto());
        var service = CreateService(httpClient);

        // Act
        var result = await service.RunFeatureResearchAsync(
            "AAPL", "momentum_5m", "2024-01-01", "2024-01-31");

        // Assert
        Assert.False(result.Success);
        Assert.Contains("No aggregates found", result.Error);
    }

    [Fact]
    public async Task RunFeatureResearchAsync_PythonServiceError_ThrowsException()
    {
        // Arrange
        var aggregates = CreateSampleAggregates();
        _marketDataServiceMock
            .Setup(s => s.GetOrFetchAggregatesAsync(
                "AAPL", 1, "minute", "2024-01-01", "2024-01-31", false, default))
            .ReturnsAsync(aggregates);

        var handler = new FakeHttpMessageHandler("Internal Server Error", HttpStatusCode.InternalServerError);
        var httpClient = new HttpClient(handler) { BaseAddress = new Uri("http://localhost:8000") };
        var service = CreateService(httpClient);

        // Act & Assert
        await Assert.ThrowsAsync<HttpRequestException>(() =>
            service.RunFeatureResearchAsync("AAPL", "momentum_5m", "2024-01-01", "2024-01-31"));
    }

    #endregion

    #region GetExperimentsAsync

    [Fact]
    public async Task GetExperimentsAsync_NoResults_ReturnsEmptyList()
    {
        // Arrange
        var httpClient = CreateMockHttpClient(new ResearchReportDto());
        var context = TestDbContextFactory.Create();
        var service = CreateService(httpClient, context);

        // Act
        var result = await service.GetExperimentsAsync("AAPL");

        // Assert
        Assert.Empty(result);
    }

    [Fact]
    public async Task GetExperimentsAsync_WithData_ReturnsOrderedHistory()
    {
        // Arrange
        var httpClient = CreateMockHttpClient(new ResearchReportDto());
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

        var service = CreateService(httpClient, context);

        // Act
        var result = await service.GetExperimentsAsync("AAPL");

        // Assert
        Assert.Equal(2, result.Count);
        Assert.Equal("rsi_14", result[0].FeatureName);
        Assert.Equal("momentum_5m", result[1].FeatureName);
    }

    #endregion

    #region GetExperimentAsync

    [Fact]
    public async Task GetExperimentAsync_NotFound_ReturnsNull()
    {
        // Arrange
        var httpClient = CreateMockHttpClient(new ResearchReportDto());
        var context = TestDbContextFactory.Create();
        var service = CreateService(httpClient, context);

        // Act
        var result = await service.GetExperimentAsync(999);

        // Assert
        Assert.Null(result);
    }

    [Fact]
    public async Task GetExperimentAsync_Found_ReturnsExperiment()
    {
        // Arrange
        var httpClient = CreateMockHttpClient(new ResearchReportDto());
        var context = TestDbContextFactory.Create();

        var ticker = new Ticker { Id = 1, Symbol = "AAPL", Name = "Apple Inc.", Market = "stocks" };
        context.Tickers.Add(ticker);

        var experiment = new ResearchExperiment
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
        };
        context.ResearchExperiments.Add(experiment);
        await context.SaveChangesAsync();

        var service = CreateService(httpClient, context);

        // Act
        var result = await service.GetExperimentAsync(experiment.Id);

        // Assert
        Assert.NotNull(result);
        Assert.Equal("AAPL", result.Ticker);
        Assert.Equal("momentum_5m", result.FeatureName);
        Assert.True(result.PassedValidation);
    }

    #endregion
}


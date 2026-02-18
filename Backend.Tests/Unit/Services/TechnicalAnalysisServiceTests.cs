using System.Net;
using System.Text.Json;
using Backend.Models.DTOs;
using Backend.Services.Implementation;
using Microsoft.Extensions.Logging;
using Moq;

namespace Backend.Tests.Unit.Services;

public class TechnicalAnalysisServiceTests
{
    private readonly Mock<ILogger<TechnicalAnalysisService>> _loggerMock = new();

    private static readonly JsonSerializerOptions _jsonOptions = new()
    {
        PropertyNamingPolicy = JsonNamingPolicy.SnakeCaseLower
    };

    private TechnicalAnalysisService CreateService(HttpMessageHandler handler)
    {
        var httpClient = new HttpClient(handler) { BaseAddress = new Uri("http://localhost:8000") };
        return new TechnicalAnalysisService(httpClient, _loggerMock.Object);
    }

    [Fact]
    public async Task CalculateIndicatorsAsync_Success_ReturnsIndicators()
    {
        var response = new CalculateIndicatorsResponseDto(
            Success: true,
            Ticker: "AAPL",
            Indicators: [
                new IndicatorResultDto("sma", 20, [
                    new IndicatorDataPointDto(1704067200000, 152.5m, null, null, null, null)
                ])
            ],
            Error: null
        );
        var json = JsonSerializer.Serialize(response, _jsonOptions);
        var handler = new FakeHttpMessageHandler(HttpStatusCode.OK, json);
        var service = CreateService(handler);

        var bars = new List<OhlcvBarDto>
        {
            new(1704067200000, 150m, 155m, 148m, 153m, 1000000m)
        };
        var indicators = new List<IndicatorConfigDto> { new("sma", 20) };

        var result = await service.CalculateIndicatorsAsync("AAPL", bars, indicators);

        Assert.True(result.Success);
        Assert.Single(result.Indicators);
        Assert.Equal("sma", result.Indicators[0].Name);
    }

    [Fact]
    public async Task CalculateIndicatorsAsync_PythonReturnsError_Throws()
    {
        var response = new CalculateIndicatorsResponseDto(
            Success: false, Ticker: "AAPL", Indicators: [], Error: "Computation failed"
        );
        var json = JsonSerializer.Serialize(response, _jsonOptions);
        var handler = new FakeHttpMessageHandler(HttpStatusCode.OK, json);
        var service = CreateService(handler);

        var bars = new List<OhlcvBarDto> { new(1704067200000, 150m, 155m, 148m, 153m, 1000000m) };
        var indicators = new List<IndicatorConfigDto> { new("sma", 20) };

        var ex = await Assert.ThrowsAsync<HttpRequestException>(() =>
            service.CalculateIndicatorsAsync("AAPL", bars, indicators));
        Assert.Contains("Computation failed", ex.Message);
    }

    [Fact]
    public async Task CalculateIndicatorsAsync_ServerError_Throws()
    {
        var handler = new FakeHttpMessageHandler(HttpStatusCode.InternalServerError, "error");
        var service = CreateService(handler);

        var bars = new List<OhlcvBarDto> { new(1704067200000, 150m, 155m, 148m, 153m, 1000000m) };
        var indicators = new List<IndicatorConfigDto> { new("sma", 20) };

        await Assert.ThrowsAsync<HttpRequestException>(() =>
            service.CalculateIndicatorsAsync("AAPL", bars, indicators));
    }

    [Fact]
    public async Task CalculateIndicatorsAsync_NullResponse_Throws()
    {
        var handler = new FakeHttpMessageHandler(HttpStatusCode.OK, "null");
        var service = CreateService(handler);

        var bars = new List<OhlcvBarDto> { new(1704067200000, 150m, 155m, 148m, 153m, 1000000m) };
        var indicators = new List<IndicatorConfigDto> { new("sma", 20) };

        await Assert.ThrowsAsync<HttpRequestException>(() =>
            service.CalculateIndicatorsAsync("AAPL", bars, indicators));
    }

    [Fact]
    public async Task CalculateIndicatorsAsync_PostsToCorrectEndpoint()
    {
        var response = new CalculateIndicatorsResponseDto(
            Success: true, Ticker: "AAPL",
            Indicators: [new IndicatorResultDto("sma", 20, [])],
            Error: null
        );
        var json = JsonSerializer.Serialize(response, _jsonOptions);
        var handler = new FakeHttpMessageHandler(HttpStatusCode.OK, json);
        var service = CreateService(handler);

        var bars = new List<OhlcvBarDto> { new(1704067200000, 150m, 155m, 148m, 153m, 1000000m) };
        var indicators = new List<IndicatorConfigDto> { new("sma", 20) };

        await service.CalculateIndicatorsAsync("AAPL", bars, indicators);

        Assert.Equal("/api/indicators/calculate", handler.LastRequestUri?.AbsolutePath);
    }
}

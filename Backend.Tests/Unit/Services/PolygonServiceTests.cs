using System.Net;
using System.Text.Json;
using Backend.Configuration;
using Backend.Models.DTOs.PolygonResponses;
using Backend.Services.Implementation;
using Backend.Services.Interfaces;
using Backend.Tests.Helpers;
using Microsoft.Extensions.Logging;
using Microsoft.Extensions.Options;
using Moq;

namespace Backend.Tests.Unit.Services;

public class PolygonServiceTests
{
    private readonly Mock<ILogger<PolygonService>> _loggerMock = new();
    private readonly IOptions<PolygonServiceOptions> _options =
        Options.Create(new PolygonServiceOptions { BaseUrl = "http://localhost:8000" });

    private static readonly JsonSerializerOptions _jsonOptions = new()
    {
        PropertyNamingPolicy = JsonNamingPolicy.SnakeCaseLower
    };

    private PolygonService CreateService(HttpMessageHandler handler)
    {
        var httpClient = new HttpClient(handler) { BaseAddress = new Uri("http://localhost:8000") };
        return new PolygonService(httpClient, _loggerMock.Object, _options);
    }

    private static FakeHttpMessageHandler CreateHandler(HttpStatusCode statusCode, object responseBody)
    {
        var json = JsonSerializer.Serialize(responseBody, _jsonOptions);
        return new FakeHttpMessageHandler(statusCode, json);
    }

    #region FetchAggregatesAsync

    [Fact]
    public async Task FetchAggregatesAsync_Success_ReturnsDeserializedResponse()
    {
        var response = new AggregateResponse
        {
            Success = true,
            Ticker = "AAPL",
            DataType = "aggregates",
            Data = [new AggregateData { Open = 150m, High = 155m, Low = 148m, Close = 153m, Volume = 1000000, Timestamp = 1768435200000L }],
            Summary = new DataSummary { OriginalCount = 1, CleanedCount = 1, RemovedCount = 0 }
        };
        var handler = CreateHandler(HttpStatusCode.OK, response);
        var service = CreateService(handler);

        var result = await service.FetchAggregatesAsync("AAPL", 1, "day", "2026-01-01", "2026-01-31");

        Assert.True(result.Success);
        Assert.Single(result.Data);
        Assert.Equal(150m, result.Data[0].Open);
    }

    [Fact]
    public async Task FetchAggregatesAsync_ServerError_ThrowsHttpRequestException()
    {
        var handler = new FakeHttpMessageHandler(HttpStatusCode.InternalServerError, "Internal Server Error");
        var service = CreateService(handler);

        await Assert.ThrowsAsync<HttpRequestException>(() =>
            service.FetchAggregatesAsync("AAPL", 1, "day", "2026-01-01", "2026-01-31"));
    }

    [Fact]
    public async Task FetchAggregatesAsync_PythonReturnsError_ThrowsHttpRequestException()
    {
        var response = new AggregateResponse
        {
            Success = false,
            Ticker = "BAD",
            DataType = "aggregates",
            Error = "Rate limit exceeded"
        };
        var handler = CreateHandler(HttpStatusCode.OK, response);
        var service = CreateService(handler);

        var ex = await Assert.ThrowsAsync<HttpRequestException>(() =>
            service.FetchAggregatesAsync("BAD", 1, "day", "2026-01-01", "2026-01-31"));
        Assert.Contains("Rate limit exceeded", ex.Message);
    }

    #endregion

    #region FetchOptionsChainSnapshotAsync

    [Fact]
    public async Task FetchOptionsChainSnapshotAsync_Success_ReturnsContracts()
    {
        var response = new OptionsChainSnapshotResponse
        {
            Success = true,
            Underlying = new UnderlyingSnapshotDto { Ticker = "AAPL", Price = 230m },
            Contracts = [new OptionsContractSnapshotDto
            {
                Ticker = "O:AAPL260220C00230000",
                ContractType = "call",
                StrikePrice = 230m,
                ImpliedVolatility = 0.35m,
                Greeks = new GreeksSnapshotDto { Delta = 0.5m, Gamma = 0.02m }
            }],
            Count = 1
        };
        var handler = CreateHandler(HttpStatusCode.OK, response);
        var service = CreateService(handler);

        var result = await service.FetchOptionsChainSnapshotAsync("AAPL", "2026-02-20");

        Assert.Single(result.Contracts);
        Assert.Equal("AAPL", result.Underlying!.Ticker);
        Assert.Equal(230m, result.Underlying.Price);
    }

    [Fact]
    public async Task FetchOptionsChainSnapshotAsync_ServerError_Throws()
    {
        var handler = new FakeHttpMessageHandler(HttpStatusCode.InternalServerError, "error");
        var service = CreateService(handler);

        await Assert.ThrowsAsync<HttpRequestException>(() =>
            service.FetchOptionsChainSnapshotAsync("AAPL"));
    }

    #endregion

    #region Risk-free rate (#2764)

    // Python owns the default risk-free rate. A caller that gives no rate must
    // send no rate field, so Python's request model fills its one default.

    [Fact]
    public async Task AnalyzeOptionsStrategyAsync_NoRiskFreeRate_OmitsTheField()
    {
        var handler = CreateHandler(HttpStatusCode.OK, new StrategyAnalyzeResponseDto { Success = true, Symbol = "AAPL" });
        var service = CreateService(handler);

        await service.AnalyzeOptionsStrategyAsync("AAPL", [CallLeg()], "2026-02-20", 230m);

        Assert.False(RequestCarriesRiskFreeRate(handler));
    }

    [Fact]
    public async Task AnalyzeOptionsStrategyAsync_RiskFreeRateGiven_SendsIt()
    {
        var handler = CreateHandler(HttpStatusCode.OK, new StrategyAnalyzeResponseDto { Success = true, Symbol = "AAPL" });
        var service = CreateService(handler);

        await service.AnalyzeOptionsStrategyAsync("AAPL", [CallLeg()], "2026-02-20", 230m, 0.05m);

        using var body = JsonDocument.Parse(handler.LastRequestBody!);
        Assert.Equal(0.05m, body.RootElement.GetProperty("risk_free_rate").GetDecimal());
    }

    [Fact]
    public async Task QuantLibPriceAsync_NoRiskFreeRate_OmitsTheField()
    {
        var handler = CreateHandler(HttpStatusCode.OK, new QuantLibPriceResponse { Success = true, Engine = "analytic_bs" });
        var service = CreateService(handler);

        await service.QuantLibPriceAsync(100m, 100m, null, 0.20m, "2026-02-20", "call");

        Assert.False(RequestCarriesRiskFreeRate(handler));
    }

    [Fact]
    public async Task PricingCompareAsync_NoRiskFreeRate_OmitsTheField()
    {
        var handler = CreateHandler(HttpStatusCode.OK, new PricingCompareResponse { Success = true, RiskFreeRate = 0.043m });
        var service = CreateService(handler);

        var result = await service.PricingCompareAsync(100m, 100m, 0.20m, "2026-02-20", "call");

        Assert.False(RequestCarriesRiskFreeRate(handler));
        Assert.Equal(0.043m, result.RiskFreeRate);
    }

    private static StrategyLegInput CallLeg() =>
        new() { Strike = 230m, OptionType = "call", Position = "long", Premium = 5m, Iv = 0.30m };

    private static bool RequestCarriesRiskFreeRate(FakeHttpMessageHandler handler)
    {
        using var body = JsonDocument.Parse(handler.LastRequestBody!);
        return body.RootElement.TryGetProperty("risk_free_rate", out _);
    }

    #endregion
}

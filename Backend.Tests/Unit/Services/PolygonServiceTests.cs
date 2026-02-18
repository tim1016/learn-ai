using System.Net;
using System.Text.Json;
using Backend.Configuration;
using Backend.Models.DTOs.PolygonResponses;
using Backend.Services.Implementation;
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
            Data = [new AggregateData { Open = 150m, High = 155m, Low = 148m, Close = 153m, Volume = 1000000, Timestamp = "2026-01-15T00:00:00Z" }],
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

    [Fact]
    public async Task FetchAggregatesAsync_PostsToCorrectEndpoint()
    {
        var response = new AggregateResponse
        {
            Success = true, Ticker = "AAPL", DataType = "aggregates",
            Data = [], Summary = new DataSummary()
        };
        var handler = CreateHandler(HttpStatusCode.OK, response);
        var service = CreateService(handler);

        await service.FetchAggregatesAsync("AAPL", 1, "day", "2026-01-01", "2026-01-31");

        Assert.Equal("/api/aggregates/fetch", handler.LastRequestUri?.AbsolutePath);
        Assert.Equal(HttpMethod.Post, handler.LastRequestMethod);
    }

    #endregion

    #region FetchTradesAsync

    [Fact]
    public async Task FetchTradesAsync_Success_ReturnsResponse()
    {
        var response = new TradeResponse
        {
            Success = true, Ticker = "AAPL", DataType = "trades",
            Data = [], Summary = new DataSummary { CleanedCount = 0 }
        };
        var handler = CreateHandler(HttpStatusCode.OK, response);
        var service = CreateService(handler);

        var result = await service.FetchTradesAsync("AAPL");

        Assert.True(result.Success);
    }

    [Fact]
    public async Task FetchTradesAsync_PythonReturnsError_Throws()
    {
        var response = new TradeResponse
        {
            Success = false, Ticker = "BAD", DataType = "trades", Error = "No trades found"
        };
        var handler = CreateHandler(HttpStatusCode.OK, response);
        var service = CreateService(handler);

        await Assert.ThrowsAsync<HttpRequestException>(() =>
            service.FetchTradesAsync("BAD"));
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

    #region FetchOptionsContractsAsync

    [Fact]
    public async Task FetchOptionsContractsAsync_Success_ReturnsContracts()
    {
        var response = new OptionsContractsResponse
        {
            Success = true,
            Contracts = [new OptionsContractDto
            {
                Ticker = "O:AAPL260220C00230000",
                UnderlyingTicker = "AAPL",
                ContractType = "call",
                StrikePrice = 230m,
                ExpirationDate = "2026-02-20"
            }],
            Count = 1
        };
        var handler = CreateHandler(HttpStatusCode.OK, response);
        var service = CreateService(handler);

        var result = await service.FetchOptionsContractsAsync("AAPL", contractType: "call");

        Assert.Single(result.Contracts);
        Assert.Equal("AAPL", result.Contracts[0].UnderlyingTicker);
    }

    #endregion

    #region CancellationToken

    [Fact]
    public async Task FetchAggregatesAsync_CancellationRequested_ThrowsOperationCanceled()
    {
        var response = new AggregateResponse
        {
            Success = true, Ticker = "AAPL", DataType = "aggregates",
            Data = [], Summary = new DataSummary()
        };
        var handler = CreateHandler(HttpStatusCode.OK, response);
        var service = CreateService(handler);

        var cts = new CancellationTokenSource();
        cts.Cancel();

        await Assert.ThrowsAnyAsync<OperationCanceledException>(() =>
            service.FetchAggregatesAsync("AAPL", 1, "day", "2026-01-01", "2026-01-31", cts.Token));
    }

    #endregion
}

/// <summary>
/// Fake HttpMessageHandler for testing HTTP client calls without a real server.
/// </summary>
public class FakeHttpMessageHandler : HttpMessageHandler
{
    private readonly HttpStatusCode _statusCode;
    private readonly string _responseBody;

    public Uri? LastRequestUri { get; private set; }
    public HttpMethod? LastRequestMethod { get; private set; }
    public string? LastRequestBody { get; private set; }

    public FakeHttpMessageHandler(HttpStatusCode statusCode, string responseBody)
    {
        _statusCode = statusCode;
        _responseBody = responseBody;
    }

    protected override async Task<HttpResponseMessage> SendAsync(
        HttpRequestMessage request, CancellationToken cancellationToken)
    {
        cancellationToken.ThrowIfCancellationRequested();

        LastRequestUri = request.RequestUri;
        LastRequestMethod = request.Method;
        if (request.Content != null)
            LastRequestBody = await request.Content.ReadAsStringAsync(cancellationToken);

        return new HttpResponseMessage(_statusCode)
        {
            Content = new StringContent(_responseBody, System.Text.Encoding.UTF8, "application/json")
        };
    }
}

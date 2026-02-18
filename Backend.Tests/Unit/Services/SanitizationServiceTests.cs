using System.Net;
using System.Text.Json;
using Backend.Models.DTOs;
using Backend.Services.Implementation;
using Microsoft.Extensions.Logging;
using Moq;

namespace Backend.Tests.Unit.Services;

public class SanitizationServiceTests
{
    private readonly Mock<ILogger<SanitizationService>> _loggerMock = new();

    private static readonly JsonSerializerOptions _jsonOptions = new()
    {
        PropertyNamingPolicy = JsonNamingPolicy.SnakeCaseLower
    };

    private SanitizationService CreateService(HttpMessageHandler handler)
    {
        var httpClient = new HttpClient(handler) { BaseAddress = new Uri("http://localhost:8000") };
        return new SanitizationService(httpClient, _loggerMock.Object);
    }

    [Fact]
    public async Task SanitizeAsync_EmptyInput_ReturnsEmptyList()
    {
        // Shouldn't even call HTTP
        var handler = new FakeHttpMessageHandler(HttpStatusCode.OK, "unused");
        var service = CreateService(handler);

        var result = await service.SanitizeAsync([]);

        Assert.Empty(result);
        Assert.Null(handler.LastRequestUri); // No HTTP call made
    }

    [Fact]
    public async Task SanitizeAsync_NullInput_ReturnsEmptyList()
    {
        var handler = new FakeHttpMessageHandler(HttpStatusCode.OK, "unused");
        var service = CreateService(handler);

        var result = await service.SanitizeAsync(null!);

        Assert.Empty(result);
    }

    [Fact]
    public async Task SanitizeAsync_Success_ReturnsCleanedData()
    {
        var responseDto = new SanitizeResponseDto(
            Success: true,
            Data: [new MarketDataRecord("AAPL", 150m, 155m, 148m, 153m, 1000000m, 1704067200000)],
            Summary: new SanitizeSummary(1, 1, 0, 0m),
            Error: null
        );
        var json = JsonSerializer.Serialize(responseDto, _jsonOptions);
        var handler = new FakeHttpMessageHandler(HttpStatusCode.OK, json);
        var service = CreateService(handler);

        var input = new List<MarketDataRecord>
        {
            new("AAPL", 150m, 155m, 148m, 153m, 1000000m, 1704067200000)
        };

        var result = await service.SanitizeAsync(input);

        Assert.Single(result);
        Assert.Equal("AAPL", result[0].Symbol);
    }

    [Fact]
    public async Task SanitizeAsync_PythonReturnsError_Throws()
    {
        var responseDto = new SanitizeResponseDto(
            Success: false, Data: [], Summary: null, Error: "Sanitization failed"
        );
        var json = JsonSerializer.Serialize(responseDto, _jsonOptions);
        var handler = new FakeHttpMessageHandler(HttpStatusCode.OK, json);
        var service = CreateService(handler);

        var input = new List<MarketDataRecord>
        {
            new("AAPL", 150m, 155m, 148m, 153m, 1000000m, 1704067200000)
        };

        var ex = await Assert.ThrowsAsync<HttpRequestException>(() =>
            service.SanitizeAsync(input));
        Assert.Contains("Sanitization failed", ex.Message);
    }

    [Fact]
    public async Task SanitizeAsync_ServerError_Throws()
    {
        var handler = new FakeHttpMessageHandler(HttpStatusCode.InternalServerError, "error");
        var service = CreateService(handler);

        var input = new List<MarketDataRecord>
        {
            new("AAPL", 150m, 155m, 148m, 153m, 1000000m, 1704067200000)
        };

        await Assert.ThrowsAsync<HttpRequestException>(() =>
            service.SanitizeAsync(input));
    }

    [Fact]
    public async Task SanitizeAsync_CustomQuantile_SentInRequest()
    {
        var responseDto = new SanitizeResponseDto(
            Success: true,
            Data: [new MarketDataRecord("AAPL", 150m, 155m, 148m, 153m, 1000000m, 1704067200000)],
            Summary: new SanitizeSummary(1, 1, 0, 0m),
            Error: null
        );
        var json = JsonSerializer.Serialize(responseDto, _jsonOptions);
        var handler = new FakeHttpMessageHandler(HttpStatusCode.OK, json);
        var service = CreateService(handler);

        var input = new List<MarketDataRecord>
        {
            new("AAPL", 150m, 155m, 148m, 153m, 1000000m, 1704067200000)
        };

        await service.SanitizeAsync(input, quantile: 0.95);

        Assert.Contains("0.95", handler.LastRequestBody);
    }

    [Fact]
    public async Task SanitizeAsync_PostsToCorrectEndpoint()
    {
        var responseDto = new SanitizeResponseDto(
            Success: true,
            Data: [new MarketDataRecord("AAPL", 150m, 155m, 148m, 153m, 1000000m, 1704067200000)],
            Summary: new SanitizeSummary(1, 1, 0, 0m),
            Error: null
        );
        var json = JsonSerializer.Serialize(responseDto, _jsonOptions);
        var handler = new FakeHttpMessageHandler(HttpStatusCode.OK, json);
        var service = CreateService(handler);

        var input = new List<MarketDataRecord>
        {
            new("AAPL", 150m, 155m, 148m, 153m, 1000000m, 1704067200000)
        };

        await service.SanitizeAsync(input);

        Assert.Equal("/api/sanitize", handler.LastRequestUri?.AbsolutePath);
    }
}

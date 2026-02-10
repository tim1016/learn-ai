using System.Net.Http.Json;
using Backend.Configuration;
using Backend.Models.DTOs.PolygonResponses;
using Backend.Services.Interfaces;
using Microsoft.Extensions.Options;

namespace Backend.Services.Implementation;

/// <summary>
/// HTTP client wrapper for Python Polygon service
/// Testable: HttpClient injected, can use HttpClient mocking libraries
/// </summary>
public class PolygonService : IPolygonService
{
    private readonly HttpClient _httpClient;
    private readonly ILogger<PolygonService> _logger;
    private readonly PolygonServiceOptions _options;

    public PolygonService(
        HttpClient httpClient,
        ILogger<PolygonService> logger,
        IOptions<PolygonServiceOptions> options)
    {
        _httpClient = httpClient ?? throw new ArgumentNullException(nameof(httpClient));
        _logger = logger ?? throw new ArgumentNullException(nameof(logger));
        _options = options?.Value ?? throw new ArgumentNullException(nameof(options));
    }

    public async Task<AggregateResponse> FetchAggregatesAsync(
        string ticker,
        int multiplier,
        string timespan,
        string fromDate,
        string toDate,
        CancellationToken cancellationToken = default)
    {
        try
        {
            _logger.LogInformation(
                "Fetching aggregates for {Ticker}: {FromDate} to {ToDate}",
                ticker, fromDate, toDate);

            var request = new
            {
                ticker,
                multiplier,
                timespan,
                from_date = fromDate,
                to_date = toDate,
                limit = 50000
            };

            var response = await _httpClient.PostAsJsonAsync(
                "/api/aggregates/fetch",
                request,
                cancellationToken);

            response.EnsureSuccessStatusCode();

            var result = await response.Content.ReadFromJsonAsync<AggregateResponse>(
                cancellationToken: cancellationToken);

            if (result == null)
            {
                throw new HttpRequestException("Received null response from Python service");
            }

            if (!result.Success)
            {
                throw new HttpRequestException($"Python service returned error: {result.Error}");
            }

            _logger.LogInformation(
                "Successfully fetched {Count} aggregates for {Ticker}",
                result.Summary?.CleanedCount ?? 0, ticker);

            return result;
        }
        catch (Exception ex)
        {
            _logger.LogError(ex, "Error fetching aggregates for {Ticker}", ticker);
            throw;
        }
    }

    public async Task<TradeResponse> FetchTradesAsync(
        string ticker,
        string? timestamp = null,
        int limit = 50000,
        CancellationToken cancellationToken = default)
    {
        try
        {
            _logger.LogInformation("Fetching trades for {Ticker}", ticker);

            var request = new
            {
                ticker,
                timestamp,
                limit
            };

            var response = await _httpClient.PostAsJsonAsync(
                "/api/trades/fetch",
                request,
                cancellationToken);

            response.EnsureSuccessStatusCode();

            var result = await response.Content.ReadFromJsonAsync<TradeResponse>(
                cancellationToken: cancellationToken);

            if (result == null)
            {
                throw new HttpRequestException("Received null response from Python service");
            }

            if (!result.Success)
            {
                throw new HttpRequestException($"Python service returned error: {result.Error}");
            }

            _logger.LogInformation(
                "Successfully fetched {Count} trades for {Ticker}",
                result.Summary?.CleanedCount ?? 0, ticker);

            return result;
        }
        catch (Exception ex)
        {
            _logger.LogError(ex, "Error fetching trades for {Ticker}", ticker);
            throw;
        }
    }
}

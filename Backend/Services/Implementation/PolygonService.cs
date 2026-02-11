using System.Net.Http.Json;
using System.Text.Json;
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
    private static readonly JsonSerializerOptions _jsonOptions = new()
    {
        PropertyNamingPolicy = JsonNamingPolicy.SnakeCaseLower
    };

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

            _logger.LogInformation(
                "[STEP 6 - PolygonService] Sending POST to Python: /api/aggregates/fetch, body={@Request}",
                request);

            var response = await _httpClient.PostAsJsonAsync(
                "/api/aggregates/fetch",
                request,
                cancellationToken);

            _logger.LogInformation(
                "[STEP 7 - PolygonService] Python response status: {StatusCode}",
                response.StatusCode);

            var rawBody = await response.Content.ReadAsStringAsync(cancellationToken);
            _logger.LogInformation(
                "[STEP 7.5 - PolygonService] Python raw response (first 500 chars): {Body}",
                rawBody.Length > 500 ? rawBody[..500] : rawBody);

            response.EnsureSuccessStatusCode();

            var result = JsonSerializer.Deserialize<AggregateResponse>(rawBody, _jsonOptions);

            if (result == null)
            {
                throw new HttpRequestException("Received null response from Python service");
            }

            if (!result.Success)
            {
                throw new HttpRequestException($"Python service returned error: {result.Error}");
            }

            _logger.LogInformation(
                "[STEP 8 - PolygonService] Deserialized: success={Success}, dataCount={Count}, summary={@Summary}",
                result.Success, result.Data?.Count ?? 0, result.Summary);

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
                _jsonOptions, cancellationToken);

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

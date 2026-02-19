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

    public async Task<OptionsChainSnapshotResponse> FetchOptionsChainSnapshotAsync(
        string underlyingTicker,
        string? expirationDate = null,
        CancellationToken cancellationToken = default)
    {
        try
        {
            _logger.LogInformation(
                "Fetching options chain snapshot for {Underlying}, expiration={Expiration}",
                underlyingTicker, expirationDate ?? "today");

            var request = new { underlying_ticker = underlyingTicker, expiration_date = expirationDate };

            var response = await _httpClient.PostAsJsonAsync(
                "/api/snapshot/options-chain",
                request,
                cancellationToken);

            response.EnsureSuccessStatusCode();

            var result = await response.Content.ReadFromJsonAsync<OptionsChainSnapshotResponse>(
                _jsonOptions, cancellationToken);

            if (result == null)
            {
                throw new HttpRequestException("Received null response from Python service for options chain snapshot");
            }

            _logger.LogInformation(
                "Fetched {Count} options chain snapshots for {Underlying}",
                result.Count, underlyingTicker);

            return result;
        }
        catch (Exception ex)
        {
            _logger.LogError(ex, "Error fetching options chain snapshot for {Underlying}", underlyingTicker);
            throw;
        }
    }

    public async Task<StockSnapshotResponse> FetchStockSnapshotAsync(
        string ticker,
        CancellationToken cancellationToken = default)
    {
        try
        {
            _logger.LogInformation("[Snapshot] Fetching stock snapshot for {Ticker}", ticker);

            var request = new { ticker };
            var response = await _httpClient.PostAsJsonAsync(
                "/api/snapshot/ticker", request, cancellationToken);

            response.EnsureSuccessStatusCode();

            var result = await response.Content.ReadFromJsonAsync<StockSnapshotResponse>(
                _jsonOptions, cancellationToken);

            if (result == null)
                throw new HttpRequestException("Received null response from Python service for stock snapshot");

            _logger.LogInformation("[Snapshot] Fetched snapshot for {Ticker}", ticker);
            return result;
        }
        catch (Exception ex)
        {
            _logger.LogError(ex, "[Snapshot] Error fetching stock snapshot for {Ticker}", ticker);
            throw;
        }
    }

    public async Task<StockSnapshotsResponse> FetchStockSnapshotsAsync(
        List<string>? tickers = null,
        CancellationToken cancellationToken = default)
    {
        try
        {
            _logger.LogInformation("[Snapshot] Fetching stock snapshots for {Tickers}",
                tickers != null ? string.Join(",", tickers) : "all");

            var request = new { tickers };
            var response = await _httpClient.PostAsJsonAsync(
                "/api/snapshot/market", request, cancellationToken);

            response.EnsureSuccessStatusCode();

            var result = await response.Content.ReadFromJsonAsync<StockSnapshotsResponse>(
                _jsonOptions, cancellationToken);

            if (result == null)
                throw new HttpRequestException("Received null response from Python service for stock snapshots");

            _logger.LogInformation("[Snapshot] Fetched {Count} stock snapshots", result.Count);
            return result;
        }
        catch (Exception ex)
        {
            _logger.LogError(ex, "[Snapshot] Error fetching stock snapshots");
            throw;
        }
    }

    public async Task<MarketMoversResponse> FetchMarketMoversAsync(
        string direction,
        CancellationToken cancellationToken = default)
    {
        try
        {
            _logger.LogInformation("[Snapshot] Fetching market movers: {Direction}", direction);

            var request = new { direction };
            var response = await _httpClient.PostAsJsonAsync(
                "/api/snapshot/movers", request, cancellationToken);

            response.EnsureSuccessStatusCode();

            var result = await response.Content.ReadFromJsonAsync<MarketMoversResponse>(
                _jsonOptions, cancellationToken);

            if (result == null)
                throw new HttpRequestException("Received null response from Python service for market movers");

            _logger.LogInformation("[Snapshot] Fetched {Count} {Direction}", result.Count, direction);
            return result;
        }
        catch (Exception ex)
        {
            _logger.LogError(ex, "[Snapshot] Error fetching market movers ({Direction})", direction);
            throw;
        }
    }

    public async Task<UnifiedSnapshotResponse> FetchUnifiedSnapshotAsync(
        List<string>? tickers = null,
        int limit = 10,
        CancellationToken cancellationToken = default)
    {
        try
        {
            _logger.LogInformation("[Snapshot] Fetching unified snapshots: tickers={Tickers}, limit={Limit}",
                tickers != null ? string.Join(",", tickers) : "none", limit);

            var request = new { tickers, limit };
            var response = await _httpClient.PostAsJsonAsync(
                "/api/snapshot/unified", request, cancellationToken);

            response.EnsureSuccessStatusCode();

            var result = await response.Content.ReadFromJsonAsync<UnifiedSnapshotResponse>(
                _jsonOptions, cancellationToken);

            if (result == null)
                throw new HttpRequestException("Received null response from Python service for unified snapshots");

            _logger.LogInformation("[Snapshot] Fetched {Count} unified snapshots", result.Count);
            return result;
        }
        catch (Exception ex)
        {
            _logger.LogError(ex, "[Snapshot] Error fetching unified snapshots");
            throw;
        }
    }

    public async Task<OptionsContractsResponse> FetchOptionsContractsAsync(
        string underlyingTicker,
        string? asOfDate = null,
        string? contractType = null,
        decimal? strikePriceGte = null,
        decimal? strikePriceLte = null,
        string? expirationDate = null,
        string? expirationDateGte = null,
        string? expirationDateLte = null,
        int limit = 100,
        CancellationToken cancellationToken = default)
    {
        try
        {
            _logger.LogInformation(
                "Fetching options contracts for {Underlying}, asOf={AsOf}, type={Type}, strike=[{Gte},{Lte}]",
                underlyingTicker, asOfDate, contractType, strikePriceGte, strikePriceLte);

            var request = new
            {
                underlying_ticker = underlyingTicker,
                as_of_date = asOfDate,
                contract_type = contractType,
                strike_price_gte = strikePriceGte,
                strike_price_lte = strikePriceLte,
                expiration_date = expirationDate,
                expiration_date_gte = expirationDateGte,
                expiration_date_lte = expirationDateLte,
                limit
            };

            var response = await _httpClient.PostAsJsonAsync(
                "/api/options/contracts",
                request,
                cancellationToken);

            response.EnsureSuccessStatusCode();

            var result = await response.Content.ReadFromJsonAsync<OptionsContractsResponse>(
                _jsonOptions, cancellationToken);

            if (result == null)
            {
                throw new HttpRequestException("Received null response from Python service for options contracts");
            }

            _logger.LogInformation(
                "Fetched {Count} options contracts for {Underlying}",
                result.Count, underlyingTicker);

            return result;
        }
        catch (Exception ex)
        {
            _logger.LogError(ex, "Error fetching options contracts for {Underlying}", underlyingTicker);
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

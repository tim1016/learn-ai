using System.Net.Http.Json;
using System.Text.Json;
using System.Text.Json.Serialization;
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

    // A pricing request omits an absent risk-free rate rather than sending
    // null, so Python fills its one default (#2764).
    private static readonly JsonSerializerOptions _omitNullRequestOptions = new(JsonSerializerDefaults.Web)
    {
        DefaultIgnoreCondition = JsonIgnoreCondition.WhenWritingNull
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
        bool adjusted = true,
        CancellationToken cancellationToken = default)
    {
        try
        {
            _logger.LogInformation(
                "Fetching aggregates for {Ticker}: {FromDate} to {ToDate} (adjusted={Adjusted})",
                ticker, fromDate, toDate, adjusted);

            var request = new
            {
                ticker,
                multiplier,
                timespan,
                from_date = fromDate,
                to_date = toDate,
                limit = 50000,
                adjusted
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

    public async Task<StrategyAnalyzeResponseDto> AnalyzeOptionsStrategyAsync(
        string symbol,
        List<StrategyLegInput> legs,
        string expirationDate,
        decimal spotPrice,
        decimal? riskFreeRate = null,
        StrategyAnalyzeOptions? options = null,
        CancellationToken cancellationToken = default)
    {
        try
        {
            _logger.LogInformation(
                "[Strategy] Analyzing {LegCount}-leg strategy for {Symbol}",
                legs.Count, symbol);

            // Phase 1.1: opt-in fields default to false / 0 so existing
            // callers see no behavior change.
            options ??= new StrategyAnalyzeOptions();

            var request = new
            {
                symbol,
                legs = legs.Select(l => new
                {
                    leg_id = l.LegId,
                    strike = l.Strike,
                    option_type = l.OptionType,
                    position = l.Position,
                    premium = l.Premium,
                    iv = l.Iv,
                    quantity = l.Quantity,
                }),
                expiration_date = expirationDate,
                spot_price = spotPrice,
                risk_free_rate = riskFreeRate,
                include_current_curve = options.IncludeCurrentCurve,
                include_greek_curves = options.IncludeGreekCurves,
                include_leg_diagnostics = options.IncludeLegDiagnostics,
                what_if_time_shift_days = options.WhatIfTimeShiftDays,
                what_if_iv_shift = options.WhatIfIvShift,
            };

            var response = await _httpClient.PostAsJsonAsync(
                "/api/strategy/analyze", request, _omitNullRequestOptions, cancellationToken);

            response.EnsureSuccessStatusCode();

            var result = await response.Content.ReadFromJsonAsync<StrategyAnalyzeResponseDto>(
                _jsonOptions, cancellationToken);

            if (result == null)
                throw new HttpRequestException("Received null response from Python service for strategy analysis");

            _logger.LogInformation(
                "[Strategy] Analysis complete for {Symbol}: POP={Pop}",
                symbol, result.Pop);

            return result;
        }
        catch (Exception ex)
        {
            _logger.LogError(ex, "[Strategy] Error analyzing strategy for {Symbol}", symbol);
            throw;
        }
    }

    public async Task<OptionsExpirationsResponse> FetchOptionsExpirationsAsync(
        string underlyingTicker,
        string? contractType = null,
        string? expirationDateGte = null,
        string? expirationDateLte = null,
        CancellationToken cancellationToken = default)
    {
        try
        {
            _logger.LogInformation(
                "Fetching options expirations for {Underlying}, type={Type}, range=[{Gte},{Lte}]",
                underlyingTicker, contractType, expirationDateGte, expirationDateLte);

            var request = new
            {
                underlying_ticker = underlyingTicker,
                contract_type = contractType,
                expiration_date_gte = expirationDateGte,
                expiration_date_lte = expirationDateLte,
            };

            var response = await _httpClient.PostAsJsonAsync(
                "/api/options/expirations",
                request,
                cancellationToken);

            response.EnsureSuccessStatusCode();

            var result = await response.Content.ReadFromJsonAsync<OptionsExpirationsResponse>(
                _jsonOptions, cancellationToken);

            if (result == null)
            {
                throw new HttpRequestException("Received null response from Python service for options expirations");
            }

            _logger.LogInformation(
                "Fetched {Count} expirations for {Underlying}",
                result.Count, underlyingTicker);

            return result;
        }
        catch (Exception ex)
        {
            _logger.LogError(ex, "Error fetching options expirations for {Underlying}", underlyingTicker);
            throw;
        }
    }

    public async Task<QuantLibPriceResponse> QuantLibPriceAsync(
        decimal spot,
        decimal strike,
        decimal? riskFreeRate,
        decimal volatility,
        string expirationDate,
        string optionType,
        string? evaluationDate = null,
        decimal dividendYield = 0m,
        string engine = "analytic_bs",
        CancellationToken cancellationToken = default)
    {
        try
        {
            _logger.LogInformation(
                "[QuantLib] Pricing {OptionType} S={Spot} K={Strike} σ={Vol} exp={Exp} engine={Engine}",
                optionType, spot, strike, volatility, expirationDate, engine);

            var request = new
            {
                spot = (double)spot,
                strike = (double)strike,
                risk_free_rate = (double?)riskFreeRate,
                volatility = (double)volatility,
                expiration_date = expirationDate,
                option_type = optionType,
                evaluation_date = evaluationDate,
                dividend_yield = (double)dividendYield,
                engine,
            };

            var response = await _httpClient.PostAsJsonAsync(
                "/api/quantlib/price", request, _omitNullRequestOptions, cancellationToken);
            response.EnsureSuccessStatusCode();

            var result = await response.Content.ReadFromJsonAsync<QuantLibPriceResponse>(
                _jsonOptions, cancellationToken);

            return result ?? throw new HttpRequestException("Null response from QuantLib price endpoint");
        }
        catch (Exception ex)
        {
            _logger.LogError(ex, "[QuantLib] Error pricing option");
            throw;
        }
    }

    public async Task<PricingCompareResponse> PricingCompareAsync(
        decimal spot,
        decimal strike,
        decimal volatility,
        string expirationDate,
        string optionType,
        decimal? riskFreeRate = null,
        decimal dividendYield = 0m,
        string? evaluationDate = null,
        decimal? spotMin = null,
        decimal? spotMax = null,
        int numPoints = 100,
        CancellationToken cancellationToken = default)
    {
        try
        {
            _logger.LogInformation(
                "[PricingCompare] Comparing models for {OptionType} S={Spot} K={Strike} σ={Vol} exp={Exp}",
                optionType, spot, strike, volatility, expirationDate);

            var request = new
            {
                spot = (double)spot,
                strike = (double)strike,
                volatility = (double)volatility,
                expiration_date = expirationDate,
                option_type = optionType,
                risk_free_rate = (double?)riskFreeRate,
                dividend_yield = (double)dividendYield,
                evaluation_date = evaluationDate,
                spot_min = spotMin.HasValue ? (double?)spotMin.Value : null,
                spot_max = spotMax.HasValue ? (double?)spotMax.Value : null,
                num_points = numPoints,
            };

            var response = await _httpClient.PostAsJsonAsync(
                "/api/quantlib/compare", request, _omitNullRequestOptions, cancellationToken);
            response.EnsureSuccessStatusCode();

            var result = await response.Content.ReadFromJsonAsync<PricingCompareResponse>(
                _jsonOptions, cancellationToken);

            return result ?? throw new HttpRequestException("Null response from pricing compare endpoint");
        }
        catch (Exception ex)
        {
            _logger.LogError(ex, "[PricingCompare] Error comparing pricing models");
            throw;
        }
    }
}

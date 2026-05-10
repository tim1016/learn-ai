using System.Net.Http.Json;
using System.Text.Json;
using Backend.Models.DTOs;
using Backend.Services.Interfaces;

namespace Backend.Services.Implementation;

public class TechnicalAnalysisService : ITechnicalAnalysisService
{
    private readonly HttpClient _httpClient;
    private readonly ILogger<TechnicalAnalysisService> _logger;

    private static readonly JsonSerializerOptions _jsonOptions = new()
    {
        PropertyNamingPolicy = JsonNamingPolicy.SnakeCaseLower
    };

    public TechnicalAnalysisService(HttpClient httpClient, ILogger<TechnicalAnalysisService> logger)
    {
        _httpClient = httpClient;
        _logger = logger;
    }

    public async Task<CalculateIndicatorsResponseDto> CalculateIndicatorsAsync(
        string ticker,
        List<OhlcvBarDto> bars,
        List<IndicatorConfigDto> indicators,
        CancellationToken cancellationToken = default)
    {
        _logger.LogInformation(
            "[TA] Sending {BarCount} bars and {IndicatorCount} indicators for {Ticker} to Python",
            bars.Count, indicators.Count, ticker);

        var request = new CalculateIndicatorsRequestDto(ticker, bars, indicators);

        var response = await _httpClient.PostAsJsonAsync(
            "/api/indicators/calculate", request, _jsonOptions, cancellationToken);

        response.EnsureSuccessStatusCode();

        var result = await response.Content.ReadFromJsonAsync<CalculateIndicatorsResponseDto>(
            _jsonOptions, cancellationToken);

        if (result is null || !result.Success)
        {
            var error = result?.Error ?? "Unknown error";
            _logger.LogError("[TA] Python service returned error: {Error}", error);
            throw new HttpRequestException($"Indicator calculation failed: {error}");
        }

        _logger.LogInformation(
            "[TA] Received {Count} indicator results for {Ticker}",
            result.Indicators.Count, ticker);

        return result;
    }

    public async Task<IndicatorTableResponseDto> GenerateIndicatorTableAsync(
        IndicatorTableRequestDto request,
        CancellationToken cancellationToken = default)
    {
        _logger.LogInformation(
            "[TA-TABLE] Requesting indicator table for {Symbol}: {From} to {To}",
            request.Symbol, request.FromDate, request.ToDate);

        var response = await _httpClient.PostAsJsonAsync(
            "/api/indicators/generate-table", request, _jsonOptions, cancellationToken);

        response.EnsureSuccessStatusCode();

        var result = await response.Content.ReadFromJsonAsync<IndicatorTableResponseDto>(
            _jsonOptions, cancellationToken);

        if (result is null || !result.Success)
        {
            var error = result?.Error ?? "Unknown error";
            _logger.LogError("[TA-TABLE] Python service returned error: {Error}", error);
            throw new HttpRequestException($"Indicator table generation failed: {error}");
        }

        _logger.LogInformation(
            "[TA-TABLE] Received {Rows} rows with {Cols} columns for {Symbol}",
            result.RowCount, result.Columns.Count, request.Symbol);

        return result;
    }

    public async Task<AvailableIndicatorsResponseDto> GetAvailableIndicatorsAsync(
        CancellationToken cancellationToken = default)
    {
        _logger.LogInformation("[TA] Fetching available indicators from Python service");

        var response = await _httpClient.GetAsync("/api/dataset/available", cancellationToken);
        response.EnsureSuccessStatusCode();

        var result = await response.Content.ReadFromJsonAsync<AvailableIndicatorsResponseDto>(
            _jsonOptions, cancellationToken);

        if (result is null)
        {
            throw new HttpRequestException("Failed to deserialize available indicators response");
        }

        _logger.LogInformation("[TA] Got {Total} available indicators", result.Total);
        return result;
    }
}

using System.Net.Http.Json;
using System.Text.Json;
using Backend.Models.DTOs;
using Backend.Services.Interfaces;

namespace Backend.Services.Implementation;

/// <summary>
/// HTTP client for the Python pandas-dq sanitization endpoint.
/// Sends raw market data, receives cleaned data.
/// </summary>
public class SanitizationService : ISanitizationService
{
    private readonly HttpClient _httpClient;
    private readonly ILogger<SanitizationService> _logger;

    private static readonly JsonSerializerOptions _jsonOptions = new()
    {
        PropertyNamingPolicy = JsonNamingPolicy.SnakeCaseLower
    };

    public SanitizationService(HttpClient httpClient, ILogger<SanitizationService> logger)
    {
        _httpClient = httpClient ?? throw new ArgumentNullException(nameof(httpClient));
        _logger = logger ?? throw new ArgumentNullException(nameof(logger));
    }

    public async Task<List<MarketDataRecord>> SanitizeAsync(
        List<MarketDataRecord> data,
        double quantile = 0.99,
        CancellationToken cancellationToken = default)
    {
        if (data is not { Count: > 0 })
            return [];

        _logger.LogInformation(
            "[Sanitize] Sending {Count} records to Python pandas-dq (quantile={Quantile})",
            data.Count, quantile);

        try
        {
            var request = new SanitizeRequestDto(data, quantile);

            var response = await _httpClient.PostAsJsonAsync(
                "/api/sanitize", request, _jsonOptions, cancellationToken);

            response.EnsureSuccessStatusCode();

            var result = await response.Content.ReadFromJsonAsync<SanitizeResponseDto>(
                _jsonOptions, cancellationToken);

            if (result is null || !result.Success)
            {
                var error = result?.Error ?? "Unknown error";
                _logger.LogError("[Sanitize] Python service returned error: {Error}", error);
                throw new HttpRequestException($"Sanitization failed: {error}");
            }

            _logger.LogInformation(
                "[Sanitize] Complete: {Cleaned}/{Original} records retained ({Removed} removed)",
                result.Summary?.CleanedCount, result.Summary?.OriginalCount, result.Summary?.RemovedCount);

            return result.Data;
        }
        catch (Exception ex)
        {
            _logger.LogError(ex, "[Sanitize] Error calling Python sanitization service");
            throw;
        }
    }
}

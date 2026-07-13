using System.Net.Http.Json;
using System.Text.Json;
using System.Text.Json.Serialization;
using Backend.Models.Comparison;
using Backend.Services.Interfaces;

namespace Backend.Services.Implementation;

/// <summary>
/// Typed HttpClient wrapper for Python's POST /api/lean-sidecar/compare.
///
/// Every record property carries [JsonPropertyName] so the snake_case
/// wire format round-trips correctly — same belt-and-suspenders fix
/// applied in PR #291 (commit a818b4b8) for PersistLeanRunPayload.
/// PropertyNameCaseInsensitive is set as belt-and-suspenders for
/// response deserialization.
/// </summary>
public class ComparisonService : IComparisonService
{
    private readonly HttpClient _http;
    private readonly ILogger<ComparisonService> _logger;

    private static readonly JsonSerializerOptions _jsonOpts = new()
    {
        PropertyNameCaseInsensitive = true,
        // The Python compare endpoint emits Decimal values as strings so
        // they round-trip without float drift (see reconcile_trade_lists);
        // divergence records carry left/right prices and quantities in
        // that form.
        NumberHandling = JsonNumberHandling.AllowReadingFromString,
    };

    public ComparisonService(HttpClient http, ILogger<ComparisonService> logger)
    {
        _http = http;
        _logger = logger;
    }

    public async Task<CompareTradesResponse> CompareTradesAsync(
        CompareTradesRequest request,
        CancellationToken ct = default)
    {
        _logger.LogInformation(
            "[COMPARE] Sending {Left} left + {Right} right trades to Python compare endpoint",
            request.LeftTrades.Count, request.RightTrades.Count);

        var response = await _http.PostAsJsonAsync(
            "api/lean-sidecar/compare",
            request,
            _jsonOpts,
            ct);

        response.EnsureSuccessStatusCode();

        var result = await response.Content.ReadFromJsonAsync<CompareTradesResponse>(_jsonOpts, ct);

        return result ?? throw new InvalidOperationException(
            "Python /api/lean-sidecar/compare returned a null body");
    }
}

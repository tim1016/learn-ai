using System.Net.Http.Json;
using System.Text.Json;
using System.Text.Json.Nodes;
using Backend.Models.DTOs;
using Backend.Services.Interfaces;

namespace Backend.Services.Implementation;

/// <summary>
/// Thin passthrough to PythonDataService /api/spec-strategy/backtest.
///
/// The StrategySpec model lives in Python (Pydantic) — this service
/// forwards the spec JSON without re-validating it. Validation errors
/// from Python surface as HttpRequestException via
/// EnsureSuccessStatusCode.
/// </summary>
public class SpecStrategyService : ISpecStrategyService
{
    private readonly HttpClient _httpClient;
    private readonly ILogger<SpecStrategyService> _logger;

    private static readonly JsonSerializerOptions _jsonOptions = new()
    {
        PropertyNamingPolicy = JsonNamingPolicy.SnakeCaseLower,
        PropertyNameCaseInsensitive = true,
    };

    public SpecStrategyService(HttpClient httpClient, ILogger<SpecStrategyService> logger)
    {
        _httpClient = httpClient;
        _logger = logger;
    }

    public async Task<SpecBacktestResponseDto> RunBacktestAsync(
        SpecBacktestRequestDto request,
        CancellationToken cancellationToken = default)
    {
        // Python expects { spec: <object>, start_date, end_date, initial_cash, fill_mode, commission_per_order }.
        // We parse the caller-supplied spec JSON into a JsonNode so
        // System.Text.Json round-trips it as an object rather than a string.
        JsonNode? specNode;
        try
        {
            specNode = JsonNode.Parse(request.Spec);
        }
        catch (JsonException ex)
        {
            throw new ArgumentException("spec must be valid JSON", nameof(request), ex);
        }

        var body = new JsonObject
        {
            ["spec"] = specNode,
            ["start_date"] = request.StartDate,
            ["end_date"] = request.EndDate,
            ["initial_cash"] = (double)request.InitialCash,
            ["fill_mode"] = request.FillMode,
            ["commission_per_order"] = (double)request.CommissionPerOrder,
        };

        _logger.LogInformation(
            "[SPEC] Forwarding backtest to Python: start={Start} end={End} cash={Cash}",
            request.StartDate, request.EndDate, request.InitialCash);

        var response = await _httpClient.PostAsJsonAsync(
            "/api/spec-strategy/backtest", body, _jsonOptions, cancellationToken);

        response.EnsureSuccessStatusCode();

        var result = await response.Content.ReadFromJsonAsync<SpecBacktestResponseDto>(
            _jsonOptions, cancellationToken);

        if (result is null)
        {
            throw new HttpRequestException("Python /api/spec-strategy/backtest returned empty body");
        }

        if (!result.Success)
        {
            _logger.LogWarning("[SPEC] Python returned success=false: {Error}", result.Error);
        }
        else
        {
            _logger.LogInformation(
                "[SPEC] Backtest complete: {TotalTrades} trades, win_rate={WinRate:P1}",
                result.TotalTrades, result.WinRate);
        }

        return result;
    }
}

using System.Net.Http.Json;
using System.Text.Json;
using Backend.Models.DTOs;
using Backend.Services.Interfaces;

namespace Backend.Services.Implementation;

public class LstmService : ILstmService
{
    private readonly HttpClient _httpClient;
    private readonly ILogger<LstmService> _logger;

    private static readonly JsonSerializerOptions _jsonOptions = new()
    {
        PropertyNamingPolicy = JsonNamingPolicy.SnakeCaseLower
    };

    public LstmService(HttpClient httpClient, ILogger<LstmService> logger)
    {
        _httpClient = httpClient;
        _logger = logger;
    }

    public async Task<LstmJobSubmitResponseDto> StartTrainingAsync(
        LstmTrainingConfigDto config,
        CancellationToken cancellationToken = default)
    {
        _logger.LogInformation("[LSTM] Starting training for {Ticker}", config.Ticker);

        var response = await _httpClient.PostAsJsonAsync(
            "/api/predictions/train", config, _jsonOptions, cancellationToken);

        response.EnsureSuccessStatusCode();

        var result = await response.Content.ReadFromJsonAsync<LstmJobSubmitResponseDto>(
            _jsonOptions, cancellationToken);

        if (result is null)
            throw new HttpRequestException("Failed to parse training submit response");

        _logger.LogInformation("[LSTM] Training job submitted: {JobId}", result.JobId);
        return result;
    }

    public async Task<LstmJobSubmitResponseDto> StartValidationAsync(
        LstmValidationConfigDto config,
        CancellationToken cancellationToken = default)
    {
        _logger.LogInformation("[LSTM] Starting validation for {Ticker}", config.Ticker);

        var response = await _httpClient.PostAsJsonAsync(
            "/api/predictions/validate", config, _jsonOptions, cancellationToken);

        response.EnsureSuccessStatusCode();

        var result = await response.Content.ReadFromJsonAsync<LstmJobSubmitResponseDto>(
            _jsonOptions, cancellationToken);

        if (result is null)
            throw new HttpRequestException("Failed to parse validation submit response");

        _logger.LogInformation("[LSTM] Validation job submitted: {JobId}", result.JobId);
        return result;
    }

    public async Task<LstmJobStatusResponseDto> GetJobStatusAsync(
        string jobId,
        CancellationToken cancellationToken = default)
    {
        _logger.LogInformation("[LSTM] Checking job status: {JobId}", jobId);

        var response = await _httpClient.GetAsync(
            $"/api/predictions/jobs/{jobId}", cancellationToken);

        response.EnsureSuccessStatusCode();

        // Deserialize into raw DTO first (result is JsonElement)
        var raw = await response.Content.ReadFromJsonAsync<LstmJobStatusRawDto>(
            _jsonOptions, cancellationToken);

        if (raw is null)
            throw new HttpRequestException($"Failed to parse job status for {jobId}");

        var result = new LstmJobStatusResponseDto
        {
            JobId = raw.JobId,
            Status = raw.Status,
            Error = raw.Error,
            CreatedAt = raw.CreatedAt,
            CompletedAt = raw.CompletedAt,
        };

        // Detect result type by checking for validation-specific fields
        if (raw.Result is { ValueKind: not JsonValueKind.Null } resultElement)
        {
            if (resultElement.TryGetProperty("num_folds", out _))
            {
                result.ValidateResult = resultElement.Deserialize<LstmValidateResultDto>(_jsonOptions);
            }
            else
            {
                result.TrainResult = resultElement.Deserialize<LstmTrainResultDto>(_jsonOptions);
            }
        }

        return result;
    }

    public async Task<List<LstmModelInfoDto>> GetModelsAsync(
        CancellationToken cancellationToken = default)
    {
        _logger.LogInformation("[LSTM] Fetching model list");

        var response = await _httpClient.GetAsync(
            "/api/predictions/models", cancellationToken);

        response.EnsureSuccessStatusCode();

        var result = await response.Content.ReadFromJsonAsync<List<LstmModelInfoDto>>(
            _jsonOptions, cancellationToken);

        return result ?? [];
    }
}

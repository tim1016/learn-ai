using System.Net;
using System.Text;
using System.Text.Json;
using Backend.Models.DTOs;
using Backend.Services.Implementation;
using Microsoft.Extensions.Logging;
using Moq;

namespace Backend.Tests.Unit.Services;

public class LstmServiceTests
{
    private readonly Mock<ILogger<LstmService>> _loggerMock = new();

    private static readonly JsonSerializerOptions _jsonOptions = new()
    {
        PropertyNamingPolicy = JsonNamingPolicy.SnakeCaseLower
    };

    private LstmService CreateService(HttpResponseMessage response)
    {
        var handler = new FakeHttpMessageHandler(response);
        var httpClient = new HttpClient(handler) { BaseAddress = new Uri("http://localhost:8000") };
        return new LstmService(httpClient, _loggerMock.Object);
    }

    #region StartTrainingAsync

    [Fact]
    public async Task StartTrainingAsync_Success_ReturnsJobId()
    {
        var responseBody = JsonSerializer.Serialize(
            new { job_id = "train-123", status = "submitted" }, _jsonOptions);

        var service = CreateService(new HttpResponseMessage(HttpStatusCode.OK)
        {
            Content = new StringContent(responseBody, Encoding.UTF8, "application/json")
        });

        var config = new LstmTrainingConfigDto
        {
            Ticker = "AAPL", FromDate = "2025-01-01", ToDate = "2025-12-31",
            Epochs = 50, SequenceLength = 60
        };

        var result = await service.StartTrainingAsync(config);

        Assert.Equal("train-123", result.JobId);
        Assert.Equal("submitted", result.Status);
    }

    [Fact]
    public async Task StartTrainingAsync_ServerError_ThrowsHttpRequestException()
    {
        var service = CreateService(new HttpResponseMessage(HttpStatusCode.InternalServerError));

        var config = new LstmTrainingConfigDto
        {
            Ticker = "AAPL", FromDate = "2025-01-01", ToDate = "2025-12-31"
        };

        await Assert.ThrowsAsync<HttpRequestException>(
            () => service.StartTrainingAsync(config));
    }

    #endregion

    #region StartValidationAsync

    [Fact]
    public async Task StartValidationAsync_Success_ReturnsJobId()
    {
        var responseBody = JsonSerializer.Serialize(
            new { job_id = "val-456", status = "submitted" }, _jsonOptions);

        var service = CreateService(new HttpResponseMessage(HttpStatusCode.OK)
        {
            Content = new StringContent(responseBody, Encoding.UTF8, "application/json")
        });

        var config = new LstmValidationConfigDto
        {
            Ticker = "MSFT", FromDate = "2025-01-01", ToDate = "2025-12-31",
            Folds = 5, Epochs = 20
        };

        var result = await service.StartValidationAsync(config);

        Assert.Equal("val-456", result.JobId);
        Assert.Equal("submitted", result.Status);
    }

    #endregion

    #region GetJobStatusAsync — Training Result

    [Fact]
    public async Task GetJobStatusAsync_TrainingResult_DeserializesCorrectly()
    {
        // Build a training result (no num_folds field)
        var trainResult = new
        {
            ticker = "AAPL",
            val_rmse = 0.05,
            train_rmse = 0.03,
            baseline_rmse = 0.08,
            improvement = 37.5,
            epochs_completed = 50,
            best_epoch = 42,
            model_id = "model-abc",
            actual_values = new[] { 100.0, 101.0 },
            predicted_values = new[] { 100.5, 101.2 },
            history_loss = new[] { 0.1, 0.05 },
            history_val_loss = new[] { 0.12, 0.06 },
            residuals = new[] { -0.5, 0.2 },
            stationarity_adf_pvalue = 0.01,
            stationarity_kpss_pvalue = 0.1,
            stationarity_is_stationary = true,
        };

        var raw = new
        {
            job_id = "train-123",
            status = "completed",
            result = trainResult,
            error = (string?)null,
            created_at = "2026-01-01T00:00:00Z",
            completed_at = "2026-01-01T00:05:00Z",
        };

        var responseBody = JsonSerializer.Serialize(raw, _jsonOptions);
        var service = CreateService(new HttpResponseMessage(HttpStatusCode.OK)
        {
            Content = new StringContent(responseBody, Encoding.UTF8, "application/json")
        });

        var result = await service.GetJobStatusAsync("train-123");

        Assert.Equal("train-123", result.JobId);
        Assert.Equal("completed", result.Status);
        Assert.NotNull(result.TrainResult);
        Assert.Null(result.ValidateResult);
        Assert.Equal("AAPL", result.TrainResult!.Ticker);
        Assert.Equal(0.05, result.TrainResult.ValRmse);
        Assert.Equal(42, result.TrainResult.BestEpoch);
        Assert.Equal("model-abc", result.TrainResult.ModelId);
        Assert.Equal(2, result.TrainResult.ActualValues.Count);
        Assert.True(result.TrainResult.StationarityIsStationary);
    }

    #endregion

    #region GetJobStatusAsync — Validation Result

    [Fact]
    public async Task GetJobStatusAsync_ValidationResult_DeserializesViaNumFoldsField()
    {
        // Validation result has num_folds field — this triggers the branching logic
        var validateResult = new
        {
            ticker = "MSFT",
            num_folds = 5,
            avg_rmse = 0.06,
            avg_mae = 0.04,
            avg_mape = 3.5,
            avg_directional_accuracy = 0.55,
            avg_sharpe_ratio = 0.8,
            avg_max_drawdown = 0.12,
            avg_profit_factor = 1.3,
            fold_results = new[]
            {
                new
                {
                    fold = 1, train_size = 200, test_size = 50,
                    rmse = 0.06, mae = 0.04, mape = 3.5,
                    directional_accuracy = 0.55,
                    sharpe_ratio = 0.8, max_drawdown = 0.12, profit_factor = 1.3,
                }
            }
        };

        var raw = new
        {
            job_id = "val-456",
            status = "completed",
            result = validateResult,
            error = (string?)null,
            created_at = "2026-01-01T00:00:00Z",
            completed_at = "2026-01-01T00:10:00Z",
        };

        var responseBody = JsonSerializer.Serialize(raw, _jsonOptions);
        var service = CreateService(new HttpResponseMessage(HttpStatusCode.OK)
        {
            Content = new StringContent(responseBody, Encoding.UTF8, "application/json")
        });

        var result = await service.GetJobStatusAsync("val-456");

        Assert.Equal("val-456", result.JobId);
        Assert.Equal("completed", result.Status);
        Assert.Null(result.TrainResult);
        Assert.NotNull(result.ValidateResult);
        Assert.Equal("MSFT", result.ValidateResult!.Ticker);
        Assert.Equal(5, result.ValidateResult.NumFolds);
        Assert.Equal(0.06, result.ValidateResult.AvgRmse);
        Assert.Single(result.ValidateResult.FoldResults);
        Assert.Equal(200, result.ValidateResult.FoldResults[0].TrainSize);
    }

    #endregion

    #region GetJobStatusAsync — Pending/Failed

    [Fact]
    public async Task GetJobStatusAsync_Pending_HasNoResult()
    {
        var raw = new
        {
            job_id = "pending-789",
            status = "running",
            result = (object?)null,
            error = (string?)null,
            created_at = "2026-01-01T00:00:00Z",
            completed_at = (string?)null,
        };

        var responseBody = JsonSerializer.Serialize(raw, _jsonOptions);
        var service = CreateService(new HttpResponseMessage(HttpStatusCode.OK)
        {
            Content = new StringContent(responseBody, Encoding.UTF8, "application/json")
        });

        var result = await service.GetJobStatusAsync("pending-789");

        Assert.Equal("running", result.Status);
        Assert.Null(result.TrainResult);
        Assert.Null(result.ValidateResult);
        Assert.Null(result.Error);
    }

    [Fact]
    public async Task GetJobStatusAsync_Failed_HasError()
    {
        var raw = new
        {
            job_id = "fail-999",
            status = "failed",
            result = (object?)null,
            error = "Out of memory",
            created_at = "2026-01-01T00:00:00Z",
            completed_at = "2026-01-01T00:01:00Z",
        };

        var responseBody = JsonSerializer.Serialize(raw, _jsonOptions);
        var service = CreateService(new HttpResponseMessage(HttpStatusCode.OK)
        {
            Content = new StringContent(responseBody, Encoding.UTF8, "application/json")
        });

        var result = await service.GetJobStatusAsync("fail-999");

        Assert.Equal("failed", result.Status);
        Assert.Equal("Out of memory", result.Error);
        Assert.Null(result.TrainResult);
        Assert.Null(result.ValidateResult);
    }

    #endregion

    #region GetModelsAsync

    [Fact]
    public async Task GetModelsAsync_Success_ReturnsModelList()
    {
        var models = new[]
        {
            new
            {
                model_id = "model-1", ticker = "AAPL",
                created_at = "2026-01-01", val_rmse = 0.05,
                train_rmse = 0.03, baseline_rmse = 0.08,
                improvement = 37.5, epochs_completed = 50,
                best_epoch = 42, sequence_length = 60,
                features = new[] { "close" },
            }
        };

        var responseBody = JsonSerializer.Serialize(models, _jsonOptions);
        var service = CreateService(new HttpResponseMessage(HttpStatusCode.OK)
        {
            Content = new StringContent(responseBody, Encoding.UTF8, "application/json")
        });

        var result = await service.GetModelsAsync();

        Assert.Single(result);
        Assert.Equal("model-1", result[0].ModelId);
        Assert.Equal("AAPL", result[0].Ticker);
    }

    [Fact]
    public async Task GetModelsAsync_EmptyResponse_ReturnsEmptyList()
    {
        var service = CreateService(new HttpResponseMessage(HttpStatusCode.OK)
        {
            Content = new StringContent("[]", Encoding.UTF8, "application/json")
        });

        var result = await service.GetModelsAsync();

        Assert.Empty(result);
    }

    #endregion
}

/// <summary>
/// Simple fake handler for mocking HttpClient without an external library.
/// </summary>
public class FakeHttpMessageHandler : HttpMessageHandler
{
    private readonly HttpResponseMessage _response;

    public FakeHttpMessageHandler(HttpResponseMessage response) => _response = response;

    protected override Task<HttpResponseMessage> SendAsync(
        HttpRequestMessage request, CancellationToken cancellationToken)
        => Task.FromResult(_response);
}

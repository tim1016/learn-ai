using Backend.Models.DTOs;

namespace Backend.Services.Interfaces;

public interface ILstmService
{
    Task<LstmJobSubmitResponseDto> StartTrainingAsync(
        LstmTrainingConfigDto config,
        CancellationToken cancellationToken = default);

    Task<LstmJobSubmitResponseDto> StartValidationAsync(
        LstmValidationConfigDto config,
        CancellationToken cancellationToken = default);

    Task<LstmJobStatusResponseDto> GetJobStatusAsync(
        string jobId,
        CancellationToken cancellationToken = default);

    Task<List<LstmModelInfoDto>> GetModelsAsync(
        CancellationToken cancellationToken = default);
}

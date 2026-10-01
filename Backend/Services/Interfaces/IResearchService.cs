using Backend.Models.DTOs;

namespace Backend.Services.Interfaces;

public interface IResearchService
{
    Task<List<ResearchExperimentDto>> GetExperimentsAsync(
        string ticker,
        CancellationToken cancellationToken = default);

    Task<List<SignalExperimentDto>> GetSignalExperimentsAsync(
        string ticker,
        CancellationToken cancellationToken = default);

    Task<SignalEngineReportDto?> GetSignalExperimentReportAsync(
        int id,
        CancellationToken cancellationToken = default);
}

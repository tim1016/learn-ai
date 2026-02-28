using Backend.Models.DTOs;

namespace Backend.Services.Interfaces;

public interface IResearchService
{
    Task<ResearchReportDto> RunFeatureResearchAsync(
        string ticker,
        string featureName,
        string fromDate,
        string toDate,
        string timespan = "minute",
        int multiplier = 1,
        CancellationToken cancellationToken = default);

    Task<List<ResearchExperimentDto>> GetExperimentsAsync(
        string ticker,
        CancellationToken cancellationToken = default);

    Task<ResearchExperimentDto?> GetExperimentAsync(
        int id,
        CancellationToken cancellationToken = default);

    Task<SignalEngineReportDto> RunSignalEngineAsync(
        string ticker,
        string featureName,
        string fromDate,
        string toDate,
        bool flipSign = true,
        bool regimeGateEnabled = true,
        string timespan = "minute",
        int multiplier = 1,
        bool forceRefresh = false,
        CancellationToken cancellationToken = default);
}

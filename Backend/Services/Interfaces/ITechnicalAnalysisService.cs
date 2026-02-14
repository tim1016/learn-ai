using Backend.Models.DTOs;

namespace Backend.Services.Interfaces;

public interface ITechnicalAnalysisService
{
    Task<CalculateIndicatorsResponseDto> CalculateIndicatorsAsync(
        string ticker,
        List<OhlcvBarDto> bars,
        List<IndicatorConfigDto> indicators,
        CancellationToken cancellationToken = default);
}

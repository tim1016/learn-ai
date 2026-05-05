using Backend.Models.DTOs;

namespace Backend.Services.Interfaces;

/// <summary>
/// Passthrough to PythonDataService /api/spec-strategy. The backend does
/// not model or validate the StrategySpec shape — that's the Python
/// layer's responsibility (Pydantic + JSON Schema export). Backend
/// forwards the spec JSON verbatim and surfaces the typed result.
/// </summary>
public interface ISpecStrategyService
{
    /// <summary>
    /// Run a backtest by POSTing a serialized StrategySpec JSON to
    /// /api/spec-strategy/backtest. The spec is passed through unchanged;
    /// validation errors surface as HttpRequestException.
    /// </summary>
    Task<SpecBacktestResponseDto> RunBacktestAsync(
        SpecBacktestRequestDto request,
        CancellationToken cancellationToken = default);
}

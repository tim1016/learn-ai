using Backend.Models.Portfolio;

namespace Backend.Services.Interfaces;

public interface IPortfolioValidationService
{
    Task<ValidationSuiteResult> RunValidationSuiteAsync(CancellationToken ct = default);
}

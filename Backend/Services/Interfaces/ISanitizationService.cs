using Backend.Models.DTOs;

namespace Backend.Services.Interfaces;

public interface ISanitizationService
{
    /// <summary>
    /// Send market data to the Python pandas-dq service for sanitization.
    /// Returns the cleaned data with outliers removed and missing values filled.
    /// </summary>
    Task<List<MarketDataRecord>> SanitizeAsync(
        List<MarketDataRecord> data,
        double quantile = 0.99,
        CancellationToken cancellationToken = default);
}

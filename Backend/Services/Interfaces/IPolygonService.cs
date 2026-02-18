using Backend.Models.DTOs.PolygonResponses;

namespace Backend.Services.Interfaces;

/// <summary>
/// Service for calling Python Polygon.io data service
/// Interface allows easy mocking in unit tests
/// </summary>
public interface IPolygonService
{
    /// <summary>
    /// Fetch aggregate bars (OHLCV) for a ticker
    /// </summary>
    Task<AggregateResponse> FetchAggregatesAsync(
        string ticker,
        int multiplier,
        string timespan,
        string fromDate,
        string toDate,
        CancellationToken cancellationToken = default);

    /// <summary>
    /// Fetch trade data for a ticker
    /// </summary>
    Task<TradeResponse> FetchTradesAsync(
        string ticker,
        string? timestamp = null,
        int limit = 50000,
        CancellationToken cancellationToken = default);

    /// <summary>
    /// Fetch options chain snapshot (greeks, IV, OI) for an underlying ticker.
    /// Filters to a specific expiration date (defaults to today in Python service).
    /// </summary>
    Task<OptionsChainSnapshotResponse> FetchOptionsChainSnapshotAsync(
        string underlyingTicker,
        string? expirationDate = null,
        CancellationToken cancellationToken = default);

    /// <summary>
    /// List options contracts for an underlying ticker
    /// </summary>
    Task<OptionsContractsResponse> FetchOptionsContractsAsync(
        string underlyingTicker,
        string? asOfDate = null,
        string? contractType = null,
        decimal? strikePriceGte = null,
        decimal? strikePriceLte = null,
        string? expirationDate = null,
        string? expirationDateGte = null,
        string? expirationDateLte = null,
        int limit = 100,
        CancellationToken cancellationToken = default);
}

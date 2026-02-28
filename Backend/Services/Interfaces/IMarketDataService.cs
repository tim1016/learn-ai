using Backend.Models.DTOs;
using Backend.Models.MarketData;

namespace Backend.Services.Interfaces;

public class AggregatesWithGapInfo
{
    public List<StockAggregate> Aggregates { get; set; } = [];
    public GapDetectionResult? GapDetection { get; set; }
}

/// <summary>
/// Orchestration service for fetching and storing market data
/// Interface allows mocking in tests - can test business logic without HTTP or DB
/// </summary>
public interface IMarketDataService
{
    /// <summary>
    /// Fetch aggregates from Python service and store in database
    /// Returns list of created/updated entities for verification
    /// </summary>
    Task<List<StockAggregate>> FetchAndStoreAggregatesAsync(
        string ticker,
        int multiplier,
        string timespan,
        string fromDate,
        string toDate,
        CancellationToken cancellationToken = default);

    /// <summary>
    /// Smart cache: check DB for existing aggregates, fetch from Polygon if missing.
    /// When forceRefresh is true, bypasses cache and always fetches from Polygon.
    /// Uses windowed fetching for minute/hour timespans and detects data gaps.
    /// </summary>
    Task<AggregatesWithGapInfo> GetOrFetchAggregatesAsync(
        string ticker,
        int multiplier,
        string timespan,
        string fromDate,
        string toDate,
        bool forceRefresh = false,
        CancellationToken cancellationToken = default);

    /// <summary>
    /// Get existing ticker or create new one (upsert)
    /// Separated for testability
    /// </summary>
    Task<Ticker> GetOrCreateTickerAsync(
        string symbol,
        string market,
        CancellationToken cancellationToken = default);
}

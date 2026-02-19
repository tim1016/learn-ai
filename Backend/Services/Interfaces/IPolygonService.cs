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
    /// Fetch a snapshot for a single stock ticker
    /// </summary>
    Task<StockSnapshotResponse> FetchStockSnapshotAsync(
        string ticker,
        CancellationToken cancellationToken = default);

    /// <summary>
    /// Fetch snapshots for multiple stock tickers
    /// </summary>
    Task<StockSnapshotsResponse> FetchStockSnapshotsAsync(
        List<string>? tickers = null,
        CancellationToken cancellationToken = default);

    /// <summary>
    /// Fetch top market movers (gainers or losers)
    /// </summary>
    Task<MarketMoversResponse> FetchMarketMoversAsync(
        string direction,
        CancellationToken cancellationToken = default);

    /// <summary>
    /// Fetch unified v3 snapshots with flexible filtering
    /// </summary>
    Task<UnifiedSnapshotResponse> FetchUnifiedSnapshotAsync(
        List<string>? tickers = null,
        int limit = 10,
        CancellationToken cancellationToken = default);

    /// <summary>
    /// Fetch basic info for a batch of stock tickers
    /// </summary>
    Task<TickerListResponse> FetchTickerListAsync(
        List<string> tickers,
        CancellationToken cancellationToken = default);

    /// <summary>
    /// Fetch detailed overview for a single ticker
    /// </summary>
    Task<TickerDetailResponse> FetchTickerDetailsAsync(
        string ticker,
        CancellationToken cancellationToken = default);

    /// <summary>
    /// Fetch related company tickers for a given stock
    /// </summary>
    Task<RelatedTickersResponse> FetchRelatedTickersAsync(
        string ticker,
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

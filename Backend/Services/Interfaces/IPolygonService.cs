using Backend.Models.DTOs.PolygonResponses;

namespace Backend.Services.Interfaces;

/// <summary>
/// Input type for a single leg in an options strategy.
/// Used by both the GraphQL layer and the service layer.
/// </summary>
public class StrategyLegInput
{
    public string? LegId { get; set; }
    public decimal Strike { get; set; }
    public required string OptionType { get; set; }
    public required string Position { get; set; }
    public decimal Premium { get; set; }
    public decimal Iv { get; set; }
    public int Quantity { get; set; } = 1;
}

/// <summary>
/// Optional flags + what-if knobs for AnalyzeOptionsStrategyAsync. Phase 1.1 of
/// `docs/architecture/numerical-authority-migration-plan.md`. Default values
/// preserve the pre-Phase-1.1 response shape — existing callers don't need to change.
/// </summary>
public class StrategyAnalyzeOptions
{
    public bool IncludeCurrentCurve { get; set; }
    public bool IncludeGreekCurves { get; set; }
    public bool IncludeLegDiagnostics { get; set; }
    public decimal WhatIfTimeShiftDays { get; set; }
    public decimal WhatIfIvShift { get; set; }
}

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
        bool adjusted = true,
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
    /// Analyze an options strategy: payoff curve, POP, EV, breakevens.
    /// Optional <paramref name="options"/> opts in to Phase 1.1 extensions
    /// (current-time curves, Greek curves, per-leg diagnostics).
    /// </summary>
    Task<StrategyAnalyzeResponseDto> AnalyzeOptionsStrategyAsync(
        string symbol,
        List<StrategyLegInput> legs,
        string expirationDate,
        decimal spotPrice,
        decimal riskFreeRate = 0.043m,
        StrategyAnalyzeOptions? options = null,
        CancellationToken cancellationToken = default);

    /// <summary>
    /// List unique expiration dates for an underlying ticker.
    /// </summary>
    Task<OptionsExpirationsResponse> FetchOptionsExpirationsAsync(
        string underlyingTicker,
        string? contractType = null,
        string? expirationDateGte = null,
        string? expirationDateLte = null,
        CancellationToken cancellationToken = default);

    /// <summary>
    /// Price a single option via the QuantLib engine for validation.
    /// </summary>
    Task<QuantLibPriceResponse> QuantLibPriceAsync(
        decimal spot,
        decimal strike,
        decimal riskFreeRate,
        decimal volatility,
        string expirationDate,
        string optionType,
        string? evaluationDate = null,
        decimal dividendYield = 0m,
        string engine = "analytic_bs",
        CancellationToken cancellationToken = default);

    /// <summary>
    /// Compare pricing models (Python BS, QuantLib BS) across a spot price range.
    /// </summary>
    Task<PricingCompareResponse> PricingCompareAsync(
        decimal spot,
        decimal strike,
        decimal volatility,
        string expirationDate,
        string optionType,
        decimal riskFreeRate = 0.05m,
        decimal dividendYield = 0m,
        string? evaluationDate = null,
        decimal? spotMin = null,
        decimal? spotMax = null,
        int numPoints = 100,
        CancellationToken cancellationToken = default);
}

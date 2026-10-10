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
/// Optional flags + what-if knobs for AnalyzeOptionsStrategyAsync. Default values
/// preserve the original response shape — existing callers don't need to change.
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
    /// Filters to a specific expiry anchor instant (defaults to today in Python service).
    /// </summary>
    Task<OptionsChainSnapshotResponse> FetchOptionsChainSnapshotAsync(
        string underlyingTicker,
        long? expirationMs = null,
        CancellationToken cancellationToken = default);

    /// <summary>
    /// Fetch a snapshot for a single stock ticker
    /// </summary>
    Task<StockSnapshotResponse> FetchStockSnapshotAsync(
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
        long expirationMs,
        decimal spotPrice,
        decimal? riskFreeRate = null,
        StrategyAnalyzeOptions? options = null,
        CancellationToken cancellationToken = default);

    /// <summary>
    /// List unique option expiries for an underlying ticker, as anchor instants.
    /// </summary>
    Task<OptionsExpirationsResponse> FetchOptionsExpirationsAsync(
        string underlyingTicker,
        string? contractType = null,
        long? expirationMsGte = null,
        long? expirationMsLte = null,
        CancellationToken cancellationToken = default);

    /// <summary>
    /// Price a single option via the QuantLib engine for validation.
    /// </summary>
    Task<QuantLibPriceResponse> QuantLibPriceAsync(
        decimal spot,
        decimal strike,
        decimal? riskFreeRate,
        decimal volatility,
        long expirationMs,
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
        long expirationMs,
        string optionType,
        decimal? riskFreeRate = null,
        decimal dividendYield = 0m,
        string? evaluationDate = null,
        decimal? spotMin = null,
        decimal? spotMax = null,
        int numPoints = 100,
        CancellationToken cancellationToken = default);
}

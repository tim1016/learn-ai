
using Backend.Data;
using Backend.GraphQL.Types;
using Backend.Services.Interfaces;
using Backend.Temporal;
using HotChocolate;
using Microsoft.EntityFrameworkCore;

namespace Backend.GraphQL;

public class Query
{
    #region Market Data Queries

    /// <summary>
    /// Smart query: returns cached data if available, fetches from Polygon if not.
    /// </summary>
    [GraphQLName("getOrFetchStockAggregates")]
    public async Task<SmartAggregatesResult> GetOrFetchStockAggregates(
        [Service] IMarketDataService marketDataService,
        [Service] ILogger<Query> logger,
        AppDbContext context,
        string ticker,
        string fromDate,
        string toDate,
        string timespan = "day",
        int multiplier = 1,
        bool forceRefresh = false,
        bool adjusted = true)
    {
        logger.LogInformation(
            "[STEP 3 - GraphQL] Query received: ticker={Ticker}, from={From}, to={To}, timespan={Timespan}, multiplier={Multiplier}, forceRefresh={ForceRefresh}, adjusted={Adjusted}",
            ticker, fromDate, toDate, timespan, multiplier, forceRefresh, adjusted);

        var aggregates = await marketDataService.GetOrFetchAggregatesAsync(
            ticker, multiplier, timespan, fromDate, toDate, forceRefresh, adjusted);

        logger.LogInformation(
            "[STEP 4 - GraphQL] MarketDataService returned {Count} aggregates for {Ticker}",
            aggregates.Count, ticker);

        // Fetch sanitization summary from the ticker entity
        var market = ticker.StartsWith("O:", StringComparison.OrdinalIgnoreCase) ? "options" : "stocks";
        var tickerEntity = await context.Tickers
            .FirstOrDefaultAsync(t => t.Symbol == ticker.ToUpper() && t.Market == market);

        var bars = aggregates.Select(a => new AggregateBar
        {
            Id = a.Id,
            Open = a.Open,
            High = a.High,
            Low = a.Low,
            Close = a.Close,
            Volume = a.Volume,
            VolumeWeightedAveragePrice = a.VolumeWeightedAveragePrice,
            Timestamp = UnixMs.FromUtc(a.Timestamp),
            Timespan = a.Timespan,
            Multiplier = a.Multiplier,
            TransactionCount = a.TransactionCount
        }).ToList();

        var result = new SmartAggregatesResult
        {
            Ticker = ticker.ToUpper(),
            Aggregates = bars,
            SanitizationSummary = tickerEntity?.SanitizationSummary,
        };

        logger.LogInformation(
            "[STEP 5 - GraphQL] Returning result: ticker={Ticker}, bars={Bars}",
            result.Ticker, result.Aggregates.Count);

        return result;
    }

    /// <summary>
    /// List unique expiration dates for an underlying ticker.
    /// Much faster than fetching full contracts — returns only date strings.
    /// </summary>
    [GraphQLName("getOptionsExpirations")]
    public async Task<OptionsExpirationsResult> GetOptionsExpirations(
        [Service] IPolygonService polygonService,
        [Service] ILogger<Query> logger,
        string underlyingTicker,
        string? contractType = null,
        string? expirationDateGte = null,
        string? expirationDateLte = null)
    {
        try
        {
            logger.LogInformation(
                "[Options] Expirations query: underlying={Underlying}, type={Type}, range=[{Gte},{Lte}]",
                underlyingTicker, contractType, expirationDateGte, expirationDateLte);

            var response = await polygonService.FetchOptionsExpirationsAsync(
                underlyingTicker, contractType, expirationDateGte, expirationDateLte);

            return new OptionsExpirationsResult
            {
                Success = response.Success,
                Expirations = response.Expirations,
                Count = response.Count,
                Error = response.Error,
            };
        }
        catch (Exception ex)
        {
            logger.LogError(ex, "[Options] Error fetching expirations for {Underlying}", underlyingTicker);
            return new OptionsExpirationsResult
            {
                Success = false,
                Error = ex.Message,
            };
        }
    }

    /// <summary>
    /// Fetch a live snapshot of the options chain for an underlying ticker.
    /// Returns greeks, IV, open interest, day OHLCV, and underlying price.
    /// </summary>
    [GraphQLName("getOptionsChainSnapshot")]
    public async Task<OptionsChainSnapshotResult> GetOptionsChainSnapshot(
        [Service] IPolygonService polygonService,
        [Service] ILogger<Query> logger,
        string underlyingTicker,
        string? expirationDate = null)
    {
        try
        {
            logger.LogInformation(
                "[Snapshot] Query: underlying={Underlying}, expiration={Expiration}",
                underlyingTicker, expirationDate ?? "today");

            var response = await polygonService.FetchOptionsChainSnapshotAsync(
                underlyingTicker, expirationDate);

            var underlying = response.Underlying != null
                ? new SnapshotUnderlyingResult
                {
                    Ticker = response.Underlying.Ticker,
                    Price = response.Underlying.Price,
                    Change = response.Underlying.Change,
                    ChangePercent = response.Underlying.ChangePercent,
                }
                : null;

            var contracts = response.Contracts.Select(c => new SnapshotContractResult
            {
                Ticker = c.Ticker,
                ContractType = c.ContractType,
                StrikePrice = c.StrikePrice,
                ExpirationDate = c.ExpirationDate,
                BreakEvenPrice = c.BreakEvenPrice,
                ImpliedVolatility = c.ImpliedVolatility,
                OpenInterest = c.OpenInterest,
                Greeks = c.Greeks != null ? new GreeksResult
                {
                    Delta = c.Greeks.Delta,
                    Gamma = c.Greeks.Gamma,
                    Theta = c.Greeks.Theta,
                    Vega = c.Greeks.Vega,
                } : null,
                Day = c.Day != null ? new DayResult
                {
                    Open = c.Day.Open,
                    High = c.Day.High,
                    Low = c.Day.Low,
                    Close = c.Day.Close,
                    Volume = c.Day.Volume,
                    Vwap = c.Day.Vwap,
                } : null,
                LastTrade = c.LastTrade != null ? new LastTradeResult
                {
                    Price = c.LastTrade.Price,
                    Size = c.LastTrade.Size,
                    Exchange = c.LastTrade.Exchange,
                    Timeframe = c.LastTrade.Timeframe,
                } : null,
                LastQuote = c.LastQuote != null ? new LastQuoteResult
                {
                    Bid = c.LastQuote.Bid,
                    Ask = c.LastQuote.Ask,
                    BidSize = c.LastQuote.BidSize,
                    AskSize = c.LastQuote.AskSize,
                    Midpoint = c.LastQuote.Midpoint,
                    Timeframe = c.LastQuote.Timeframe,
                } : null,
            }).ToList();

            return new OptionsChainSnapshotResult
            {
                Success = true,
                Underlying = underlying,
                Contracts = contracts,
                Count = contracts.Count,
                RiskFreeRate = response.RiskFreeRate,
                DividendYield = response.DividendYield,
                RateSource = response.RateSource,
                DividendSource = response.DividendSource,
            };
        }
        catch (Exception ex)
        {
            logger.LogError(ex, "[Snapshot] Error fetching chain for {Underlying}", underlyingTicker);
            return new OptionsChainSnapshotResult
            {
                Success = false,
                Error = ex.Message,
            };
        }
    }

    /// <summary>
    /// Fetch a snapshot for a single stock ticker.
    /// Returns price, day/prevDay OHLCV, and today's change.
    /// </summary>
    [GraphQLName("getStockSnapshot")]
    public async Task<StockSnapshotResult> GetStockSnapshot(
        [Service] IPolygonService polygonService,
        [Service] ILogger<Query> logger,
        string ticker)
    {
        try
        {
            logger.LogInformation("[Snapshot] Query: ticker={Ticker}", ticker);

            var response = await polygonService.FetchStockSnapshotAsync(ticker);

            return new StockSnapshotResult
            {
                Success = response.Success,
                Snapshot = response.Snapshot != null
                    ? MapTickerSnapshot(response.Snapshot)
                    : null,
                Error = response.Error,
            };
        }
        catch (Exception ex)
        {
            logger.LogError(ex, "[Snapshot] Error fetching stock snapshot for {Ticker}", ticker);
            return new StockSnapshotResult { Success = false, Error = ex.Message };
        }
    }

    private static StockTickerSnapshotResult MapTickerSnapshot(
        Backend.Models.DTOs.PolygonResponses.StockTickerSnapshotDto dto) => new()
        {
            Ticker = dto.Ticker,
            Day = dto.Day != null ? new SnapshotBarResult
            {
                Open = dto.Day.Open,
                High = dto.Day.High,
                Low = dto.Day.Low,
                Close = dto.Day.Close,
                Volume = dto.Day.Volume,
                Vwap = dto.Day.Vwap,
            } : null,
            PrevDay = dto.PrevDay != null ? new SnapshotBarResult
            {
                Open = dto.PrevDay.Open,
                High = dto.PrevDay.High,
                Low = dto.PrevDay.Low,
                Close = dto.PrevDay.Close,
                Volume = dto.PrevDay.Volume,
                Vwap = dto.PrevDay.Vwap,
            } : null,
            Min = dto.Min != null ? new MinuteBarResult
            {
                Open = dto.Min.Open,
                High = dto.Min.High,
                Low = dto.Min.Low,
                Close = dto.Min.Close,
                Volume = dto.Min.Volume,
                Vwap = dto.Min.Vwap,
                AccumulatedVolume = dto.Min.AccumulatedVolume,
                Timestamp = dto.Min.Timestamp,
            } : null,
            TodaysChange = dto.TodaysChange,
            TodaysChangePercent = dto.TodaysChangePercent,
            Updated = dto.Updated,
        };

    /// <summary>
    /// Analyze an options strategy: payoff curve, POP, EV, max profit/loss, breakevens.
    /// All probability math is computed server-side in Python using Black-Scholes.
    /// </summary>
    [GraphQLName("analyzeOptionsStrategy")]
    public async Task<StrategyAnalyzeResult> AnalyzeOptionsStrategy(
        [Service] IPolygonService polygonService,
        [Service] ILogger<Query> logger,
        string symbol,
        List<StrategyLegInput> legs,
        string expirationDate,
        decimal spotPrice,
        decimal riskFreeRate = 0.043m,
        bool includeCurrentCurve = false,
        bool includeGreekCurves = false,
        bool includeLegDiagnostics = false,
        decimal whatIfTimeShiftDays = 0m,
        decimal whatIfIvShift = 0m)
    {
        try
        {
            logger.LogInformation(
                "[Strategy] GraphQL query: symbol={Symbol}, legs={LegCount}, expiration={Expiration}",
                symbol, legs.Count, expirationDate);

            var options = new StrategyAnalyzeOptions
            {
                IncludeCurrentCurve = includeCurrentCurve,
                IncludeGreekCurves = includeGreekCurves,
                IncludeLegDiagnostics = includeLegDiagnostics,
                WhatIfTimeShiftDays = whatIfTimeShiftDays,
                WhatIfIvShift = whatIfIvShift,
            };

            var response = await polygonService.AnalyzeOptionsStrategyAsync(
                symbol, legs, expirationDate, spotPrice, riskFreeRate, options);

            return new StrategyAnalyzeResult
            {
                Success = response.Success,
                Symbol = response.Symbol,
                SpotPrice = response.SpotPrice,
                StrategyCost = response.StrategyCost,
                Pop = response.Pop,
                ExpectedValue = response.ExpectedValue,
                MaxProfit = response.MaxProfit,
                MaxLoss = response.MaxLoss,
                Breakevens = response.Breakevens,
                Curve = response.Curve.Select(p => new PayoffPointResult
                {
                    Price = p.Price,
                    Pnl = p.Pnl,
                }).ToList(),
                Greeks = new StrategyGreeksResult
                {
                    Delta = response.Greeks.Delta,
                    Gamma = response.Greeks.Gamma,
                    Theta = response.Greeks.Theta,
                    Vega = response.Greeks.Vega,
                },
                CurrentCurve = response.CurrentCurve?.Select(p => new CurrentCurvePointResult
                {
                    Price = p.Price,
                    TheoreticalValue = p.TheoreticalValue,
                    TheoreticalPnl = p.TheoreticalPnl,
                }).ToList(),
                GreekCurves = response.GreekCurves?.Select(p => new GreekCurvePointResult
                {
                    Price = p.Price,
                    Delta = p.Delta,
                    Gamma = p.Gamma,
                    Theta = p.Theta,
                    Vega = p.Vega,
                }).ToList(),
                LegDiagnostics = response.LegDiagnostics?.Select(d => new LegDiagnosticResult
                {
                    LegId = d.LegId,
                    Strike = d.Strike,
                    OptionType = d.OptionType,
                    Position = d.Position,
                    Quantity = d.Quantity,
                    Iv = d.Iv,
                    EntryPremium = d.EntryPremium,
                    CurrentTheoretical = d.CurrentTheoretical,
                    CurrentDelta = d.CurrentDelta,
                    CurrentGamma = d.CurrentGamma,
                    CurrentTheta = d.CurrentTheta,
                    CurrentVega = d.CurrentVega,
                    LegPnl = d.LegPnl,
                }).ToList(),
                Error = response.Error,
            };
        }
        catch (Exception ex)
        {
            logger.LogError(ex, "[Strategy] Error analyzing strategy for {Symbol}", symbol);
            return new StrategyAnalyzeResult
            {
                Success = false,
                Symbol = symbol,
                Error = ex.Message,
            };
        }
    }

    #endregion

    #region Research Lab Queries

    [GraphQLName("getResearchExperiments")]
    public async Task<List<ResearchExperimentType>> GetResearchExperiments(
        [Service] IResearchService researchService,
        string ticker)
    {
        var experiments = await researchService.GetExperimentsAsync(ticker);

        return experiments.Select(e => new ResearchExperimentType
        {
            Id = e.Id,
            Ticker = e.Ticker,
            FeatureName = e.FeatureName,
            StartDate = e.StartDate,
            EndDate = e.EndDate,
            BarsUsed = e.BarsUsed,
            MeanIC = e.MeanIC,
            ICTStat = e.ICTStat,
            ICPValue = e.ICPValue,
            AdfPValue = e.AdfPValue,
            KpssPValue = e.KpssPValue,
            IsStationary = e.IsStationary,
            PassedValidation = e.PassedValidation,
            MonotonicityRatio = e.MonotonicityRatio,
            IsMonotonic = e.IsMonotonic,
            CreatedAt = e.CreatedAt,
        }).ToList();
    }

    [GraphQLName("getSignalExperiments")]
    public async Task<List<SignalExperimentType>> GetSignalExperiments(
        [Service] IResearchService researchService,
        string ticker)
    {
        var experiments = await researchService.GetSignalExperimentsAsync(ticker);

        return experiments.Select(e => new SignalExperimentType
        {
            Id = e.Id,
            Ticker = e.Ticker,
            FeatureName = e.FeatureName,
            StartDate = e.StartDate,
            EndDate = e.EndDate,
            BarsUsed = e.BarsUsed,
            OverallGrade = e.OverallGrade,
            StatusLabel = e.StatusLabel,
            OverallPassed = e.OverallPassed,
            MeanOosSharpe = e.MeanOosSharpe,
            BestThreshold = e.BestThreshold,
            BestCostBps = e.BestCostBps,
            FlipSign = e.FlipSign,
            RegimeGateEnabled = e.RegimeGateEnabled,
            CreatedAt = e.CreatedAt,
        }).ToList();
    }

    [GraphQLName("getSignalExperimentReport")]
    public async Task<SignalEngineResultType?> GetSignalExperimentReport(
        [Service] IResearchService researchService,
        int id)
    {
        var report = await researchService.GetSignalExperimentReportAsync(id);
        if (report is null) return null;

        return SignalResultMapper.ToGraphQL(report);
    }

    #endregion

    #region QuantLib Validation Queries

    /// <summary>
    /// Price a single option via QuantLib for validation against legacy BS.
    /// Returns theoretical price and all five Greeks.
    /// </summary>
    [GraphQLName("quantlibPrice")]
    public async Task<QuantLibPriceResult> QuantLibPrice(
        [Service] IPolygonService polygonService,
        [Service] ILogger<Query> logger,
        decimal spot,
        decimal strike,
        decimal volatility,
        string expirationDate,
        string optionType,
        decimal riskFreeRate = 0.05m,
        string? evaluationDate = null,
        decimal dividendYield = 0m,
        string engine = "analytic_bs")
    {
        try
        {
            var response = await polygonService.QuantLibPriceAsync(
                spot, strike, riskFreeRate, volatility, expirationDate,
                optionType, evaluationDate, dividendYield, engine);

            return new QuantLibPriceResult
            {
                Success = response.Success,
                Engine = response.Engine,
                Price = response.Price,
                Delta = response.Delta,
                Gamma = response.Gamma,
                Theta = response.Theta,
                Vega = response.Vega,
                Rho = response.Rho,
                D1 = response.D1,
                D2 = response.D2,
                Error = response.Error,
            };
        }
        catch (Exception ex)
        {
            logger.LogError(ex, "[QuantLib] Error pricing option");
            return new QuantLibPriceResult { Success = false, Error = ex.Message };
        }
    }

    #endregion

    #region Pricing Model Comparison

    /// <summary>
    /// Compare pricing models (Legacy JS BS, Python BS, QuantLib BS) across a spot range.
    /// Returns price and Greek curves for each model to plot side-by-side.
    /// </summary>
    [GraphQLName("pricingModelComparison")]
    public async Task<PricingCompareResult> PricingModelComparison(
        [Service] IPolygonService polygonService,
        [Service] ILogger<Query> logger,
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
        int numPoints = 100)
    {
        try
        {
            var response = await polygonService.PricingCompareAsync(
                spot, strike, volatility, expirationDate, optionType,
                riskFreeRate, dividendYield, evaluationDate,
                spotMin, spotMax, numPoints);

            return new PricingCompareResult
            {
                Success = response.Success,
                Strike = response.Strike,
                OptionType = response.OptionType,
                ExpirationDate = response.ExpirationDate,
                TimeToExpiryYears = response.TimeToExpiryYears,
                Models = response.Models.Select(m => new PricingModelCurveResult
                {
                    Model = m.Model,
                    Points = m.Points.Select(p => new PricingPointGql
                    {
                        Spot = p.Spot,
                        Price = p.Price,
                        Delta = p.Delta,
                        Gamma = p.Gamma,
                        Theta = p.Theta,
                        Vega = p.Vega,
                        Rho = p.Rho,
                    }).ToList(),
                }).ToList(),
                Error = response.Error,
            };
        }
        catch (Exception ex)
        {
            logger.LogError(ex, "[PricingCompare] Error comparing pricing models");
            return new PricingCompareResult { Success = false, Error = ex.Message };
        }
    }

    #endregion
}

public class OptionsChainSnapshotResult
{
    public bool Success { get; set; }
    public SnapshotUnderlyingResult? Underlying { get; set; }
    public List<SnapshotContractResult> Contracts { get; set; } = [];
    public int Count { get; set; }
    public decimal? RiskFreeRate { get; set; }
    public decimal? DividendYield { get; set; }
    public string? RateSource { get; set; }
    public string? DividendSource { get; set; }
    public string? Error { get; set; }
}

public class SnapshotUnderlyingResult
{
    public string Ticker { get; set; } = "";
    public decimal Price { get; set; }
    public decimal Change { get; set; }
    public decimal ChangePercent { get; set; }
}

public class SnapshotContractResult
{
    public string? Ticker { get; set; }
    public string? ContractType { get; set; }
    public decimal? StrikePrice { get; set; }
    public string? ExpirationDate { get; set; }
    public decimal? BreakEvenPrice { get; set; }
    public decimal? ImpliedVolatility { get; set; }
    public decimal? OpenInterest { get; set; }
    public GreeksResult? Greeks { get; set; }
    public DayResult? Day { get; set; }
    public LastTradeResult? LastTrade { get; set; }
    public LastQuoteResult? LastQuote { get; set; }
}

public class GreeksResult
{
    public decimal? Delta { get; set; }
    public decimal? Gamma { get; set; }
    public decimal? Theta { get; set; }
    public decimal? Vega { get; set; }
}

public class DayResult
{
    public decimal? Open { get; set; }
    public decimal? High { get; set; }
    public decimal? Low { get; set; }
    public decimal? Close { get; set; }
    public decimal? Volume { get; set; }
    public decimal? Vwap { get; set; }
}

public class LastTradeResult
{
    public decimal? Price { get; set; }
    public decimal? Size { get; set; }
    public int? Exchange { get; set; }
    public string? Timeframe { get; set; }
}

public class LastQuoteResult
{
    public decimal? Bid { get; set; }
    public decimal? Ask { get; set; }
    public decimal? BidSize { get; set; }
    public decimal? AskSize { get; set; }
    public decimal? Midpoint { get; set; }
    public string? Timeframe { get; set; }
}

public class OptionsExpirationsResult
{
    public bool Success { get; set; }
    public List<string> Expirations { get; set; } = [];
    public int Count { get; set; }
    public string? Error { get; set; }
}

// ------------------------------------------------------------------
// Stock Snapshot result types
// ------------------------------------------------------------------

public class SnapshotBarResult
{
    public decimal? Open { get; set; }
    public decimal? High { get; set; }
    public decimal? Low { get; set; }
    public decimal? Close { get; set; }
    public decimal? Volume { get; set; }
    public decimal? Vwap { get; set; }
}

public class MinuteBarResult : SnapshotBarResult
{
    public decimal? AccumulatedVolume { get; set; }
    public long? Timestamp { get; set; }
}

public class StockTickerSnapshotResult
{
    public string? Ticker { get; set; }
    public SnapshotBarResult? Day { get; set; }
    public SnapshotBarResult? PrevDay { get; set; }
    public MinuteBarResult? Min { get; set; }
    public decimal? TodaysChange { get; set; }
    public decimal? TodaysChangePercent { get; set; }
    public long? Updated { get; set; }
}

public class StockSnapshotResult
{
    public bool Success { get; set; }
    public StockTickerSnapshotResult? Snapshot { get; set; }
    public string? Error { get; set; }
}

// ------------------------------------------------------------------
// Ticker Reference result types
// ------------------------------------------------------------------

// ------------------------------------------------------------------
// Options Strategy Analysis result types
// ------------------------------------------------------------------

public class PayoffPointResult
{
    public decimal Price { get; set; }

    [GraphQLName("pnl")]
    public decimal Pnl { get; set; }
}

public class StrategyGreeksResult
{
    public decimal Delta { get; set; }
    public decimal Gamma { get; set; }
    public decimal Theta { get; set; }
    public decimal Vega { get; set; }
}

public class StrategyAnalyzeResult
{
    public bool Success { get; set; }
    public string Symbol { get; set; } = "";
    public decimal SpotPrice { get; set; }
    public decimal StrategyCost { get; set; }
    public decimal Pop { get; set; }
    public decimal ExpectedValue { get; set; }
    public decimal MaxProfit { get; set; }
    public decimal MaxLoss { get; set; }
    public List<decimal> Breakevens { get; set; } = [];
    public List<PayoffPointResult> Curve { get; set; } = [];
    public StrategyGreeksResult Greeks { get; set; } = new();

    // Phase 1.1 opt-in extensions. Null when the corresponding include_*
    // request flag was false (the default). Explicit GraphQLName to follow
    // the file's convention (HC v15 camelCase inference is fine for these
    // names, but explicit > implicit makes refactor renames safe).
    [GraphQLName("currentCurve")]
    public List<CurrentCurvePointResult>? CurrentCurve { get; set; }

    [GraphQLName("greekCurves")]
    public List<GreekCurvePointResult>? GreekCurves { get; set; }

    [GraphQLName("legDiagnostics")]
    public List<LegDiagnosticResult>? LegDiagnostics { get; set; }

    public string? Error { get; set; }
}

public class CurrentCurvePointResult
{
    public decimal Price { get; set; }
    public decimal TheoreticalValue { get; set; }

    [GraphQLName("theoreticalPnl")]
    public decimal TheoreticalPnl { get; set; }
}

public class GreekCurvePointResult
{
    public decimal Price { get; set; }
    public decimal Delta { get; set; }
    public decimal Gamma { get; set; }
    public decimal Theta { get; set; }
    public decimal Vega { get; set; }
}

public class LegDiagnosticResult
{
    public string? LegId { get; set; }
    public decimal Strike { get; set; }
    public string OptionType { get; set; } = "";
    public string Position { get; set; } = "";
    public int Quantity { get; set; }
    public decimal Iv { get; set; }
    public decimal EntryPremium { get; set; }
    public decimal CurrentTheoretical { get; set; }
    public decimal CurrentDelta { get; set; }
    public decimal CurrentGamma { get; set; }
    public decimal CurrentTheta { get; set; }
    public decimal CurrentVega { get; set; }

    [GraphQLName("legPnl")]
    public decimal LegPnl { get; set; }
}

// ------------------------------------------------------------------
// QuantLib Validation result types
// ------------------------------------------------------------------

public class QuantLibPriceResult
{
    public bool Success { get; set; }
    public string Engine { get; set; } = "";
    public decimal Price { get; set; }
    public decimal Delta { get; set; }
    public decimal Gamma { get; set; }
    public decimal Theta { get; set; }
    public decimal Vega { get; set; }
    public decimal Rho { get; set; }
    public decimal? D1 { get; set; }
    public decimal? D2 { get; set; }
    public string? Error { get; set; }
}

// ------------------------------------------------------------------
// Pricing model comparison result types
// ------------------------------------------------------------------

public class PricingPointGql
{
    public decimal Spot { get; set; }
    public decimal Price { get; set; }
    public decimal Delta { get; set; }
    public decimal Gamma { get; set; }
    public decimal Theta { get; set; }
    public decimal Vega { get; set; }
    public decimal Rho { get; set; }
}

public class PricingModelCurveResult
{
    public string Model { get; set; } = "";
    public List<PricingPointGql> Points { get; set; } = [];
}

public class PricingCompareResult
{
    public bool Success { get; set; }
    public decimal Strike { get; set; }
    public string OptionType { get; set; } = "";
    public string ExpirationDate { get; set; } = "";
    public decimal TimeToExpiryYears { get; set; }
    public List<PricingModelCurveResult> Models { get; set; } = [];
    public string? Error { get; set; }
}

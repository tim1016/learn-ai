using Backend.Data;
using Backend.GraphQL.Types;
using Backend.Models.DTOs;
using Backend.Models.MarketData;
using Backend.Services.Implementation;
using Backend.Services.Interfaces;
using Backend.Temporal;
using HotChocolate;
using Microsoft.EntityFrameworkCore;

namespace Backend.GraphQL;

public class Mutation
{
    #region Market Data Mutations

    /// <summary>
    /// Fetch stock aggregate data (OHLCV bars) from Polygon.io
    /// Testable: IMarketDataService injected via [Service] attribute
    /// </summary>
    /// <param name="marketDataService">Injected service (mockable in tests)</param>
    /// <param name="ticker">Stock symbol (e.g., AAPL, MSFT)</param>
    /// <param name="fromDate">Start date (YYYY-MM-DD)</param>
    /// <param name="toDate">End date (YYYY-MM-DD)</param>
    /// <param name="timespan">Time window: minute, hour, day, week, month</param>
    /// <param name="multiplier">Timespan multiplier (e.g., 5 for 5-minute bars)</param>
    public async Task<FetchAggregatesResult> FetchStockAggregates(
        [Service] IMarketDataService marketDataService,
        string ticker,
        string fromDate,
        string toDate,
        string timespan = "day",
        int multiplier = 1)
    {
        try
        {
            var aggregates = await marketDataService.FetchAndStoreAggregatesAsync(
                ticker, multiplier, timespan, fromDate, toDate);

            return new FetchAggregatesResult
            {
                Success = true,
                Ticker = ticker,
                Count = aggregates.Count,
                Message = $"Successfully fetched and stored {aggregates.Count} aggregates for {ticker}"
            };
        }
        catch (Exception ex)
        {
            return new FetchAggregatesResult
            {
                Success = false,
                Ticker = ticker,
                Count = 0,
                Message = $"Error: {ex.Message}"
            };
        }
    }

    /// <summary>
    /// Sanitize raw market data using the Python pandas-dq service.
    /// Removes outliers, fills missing values, and enforces data types.
    /// </summary>
    public async Task<SanitizeMarketDataResult> SanitizeMarketData(
        [Service] ISanitizationService sanitizationService,
        List<MarketDataRecord> data,
        double quantile = 0.99)
    {
        try
        {
            var cleaned = await sanitizationService.SanitizeAsync(data, quantile);

            return new SanitizeMarketDataResult
            {
                Success = true,
                Data = cleaned,
                OriginalCount = data.Count,
                CleanedCount = cleaned.Count,
                Message = $"Sanitized {data.Count} records → {cleaned.Count} retained"
            };
        }
        catch (Exception ex)
        {
            return new SanitizeMarketDataResult
            {
                Success = false,
                Data = [],
                OriginalCount = data.Count,
                CleanedCount = 0,
                Message = $"Error: {ex.Message}"
            };
        }
    }

    #endregion

    #region Research Lab Mutations

    [GraphQLName("runFeatureResearch")]
    public async Task<ResearchResultType> RunFeatureResearch(
        [Service] IResearchService researchService,
        [Service] ILogger<Mutation> logger,
        string ticker,
        string featureName,
        string fromDate,
        string toDate,
        string timespan = "minute",
        int multiplier = 1)
    {
        try
        {
            logger.LogInformation(
                "[Research] Running {Feature} on {Ticker} from {From} to {To}",
                featureName, ticker, fromDate, toDate);

            var report = await researchService.RunFeatureResearchAsync(
                ticker, featureName, fromDate, toDate, timespan, multiplier);

            return ResearchResultMapper.ToGraphQL(report);
        }
        catch (Exception ex)
        {
            logger.LogError(ex, "[Research] Error running {Feature} on {Ticker}", featureName, ticker);
            return new ResearchResultType
            {
                Success = false,
                Ticker = ticker,
                FeatureName = featureName,
                Error = ex.Message,
            };
        }
    }

    [GraphQLName("runSignalEngine")]
    public async Task<SignalEngineResultType> RunSignalEngine(
        [Service] IResearchService researchService,
        [Service] ILogger<Mutation> logger,
        string ticker,
        string featureName = "momentum_5m",
        string fromDate = "",
        string toDate = "",
        bool flipSign = true,
        bool regimeGateEnabled = true,
        string timespan = "minute",
        int multiplier = 1,
        bool forceRefresh = false)
    {
        try
        {
            logger.LogInformation(
                "[Signal] Running {Feature} on {Ticker} from {From} to {To}",
                featureName, ticker, fromDate, toDate);

            var report = await researchService.RunSignalEngineAsync(
                ticker, featureName, fromDate, toDate,
                flipSign, regimeGateEnabled, timespan, multiplier, forceRefresh);

            return SignalResultMapper.ToGraphQL(report);
        }
        catch (Exception ex)
        {
            logger.LogError(ex, "[Signal] Error running {Feature} on {Ticker}", featureName, ticker);
            return new SignalEngineResultType
            {
                Success = false,
                Ticker = ticker,
                FeatureName = featureName,
                Error = ex.Message,
            };
        }
    }

    [GraphQLName("runOptionsFeatureResearch")]
    public async Task<ResearchResultType> RunOptionsFeatureResearch(
        [Service] IResearchService researchService,
        [Service] ILogger<Mutation> logger,
        string ticker,
        string featureName,
        string fromDate,
        string toDate,
        string targetType = "directional")
    {
        try
        {
            logger.LogInformation(
                "[Options Research] Running {Feature} on {Ticker} from {From} to {To} (target={Target})",
                featureName, ticker, fromDate, toDate, targetType);

            var report = await researchService.RunOptionsFeatureResearchAsync(
                ticker, featureName, fromDate, toDate, targetType);

            return ResearchResultMapper.ToGraphQL(report);
        }
        catch (Exception ex)
        {
            logger.LogError(ex, "[Options Research] Error running {Feature} on {Ticker}", featureName, ticker);
            return new ResearchResultType
            {
                Success = false,
                Ticker = ticker,
                FeatureName = featureName,
                Error = ex.Message,
            };
        }
    }

    [GraphQLName("runBatchOptionsResearch")]
    public async Task<BatchResearchResultType> RunBatchOptionsResearch(
        [Service] IResearchService researchService,
        [Service] ILogger<Mutation> logger,
        string featureName,
        List<string> tickers,
        string fromDate,
        string toDate,
        string targetType = "directional")
    {
        try
        {
            logger.LogInformation(
                "[Batch Options] Running {Feature} across {Count} tickers",
                featureName, tickers.Count);

            var report = await researchService.RunBatchOptionsResearchAsync(
                featureName, tickers, fromDate, toDate, targetType);

            return new BatchResearchResultType
            {
                Success = report.Success,
                FeatureName = report.FeatureName,
                TickersTested = report.TickersTested,
                TickersPassed = report.TickersPassed,
                PassRate = report.PassRate,
                CrossSectionalConsistent = report.CrossSectionalConsistent,
                AggregateIc = report.AggregateIc,
                TickerResults = report.TickerResults.Select(tr => new TickerBatchResultType
                {
                    Ticker = tr.Ticker,
                    MeanIc = tr.MeanIc,
                    IcTStat = tr.IcTStat,
                    IcPValue = tr.IcPValue,
                    NwTStat = tr.NwTStat,
                    NwPValue = tr.NwPValue,
                    EffectiveN = tr.EffectiveN,
                    IsStationary = tr.IsStationary,
                    PassedValidation = tr.PassedValidation,
                    DataPoints = tr.DataPoints,
                    Error = tr.Error,
                }).ToList(),
                Summary = report.Summary,
                Error = report.Error,
            };
        }
        catch (Exception ex)
        {
            logger.LogError(ex, "[Batch Options] Error running {Feature}", featureName);
            return new BatchResearchResultType
            {
                Success = false,
                FeatureName = featureName,
                Error = ex.Message,
            };
        }
    }

    #endregion

    #region Rule-Based Backtest Mutations

    /// <summary>
    /// Run a configurable rule-based backtest via the Python service.
    /// Supports EMA crossover + RSI filter + gap filter + fixed-bar exit.
    /// </summary>
    [GraphQLName("runRuleBasedBacktest")]
    public async Task<RuleBasedBacktestResultType> RunRuleBasedBacktest(
        [Service] IPolygonService polygonService,
        [Service] ILogger<Mutation> logger,
        string ticker,
        string fromDate,
        string toDate,
        int multiplier = 15,
        string timespan = "minute",
        bool filterRth = true,
        string parametersJson = "{}")
    {
        try
        {
            logger.LogInformation(
                "[RuleBasedBacktest] Running on {Ticker} {Multiplier}×{Timespan} from {From} to {To}",
                ticker, multiplier, timespan, fromDate, toDate);

            var requestBody = new
            {
                ticker = ticker.ToUpper(),
                from_date = fromDate,
                to_date = toDate,
                multiplier = multiplier,
                timespan = timespan,
                filter_rth = filterRth,
                parameters = System.Text.Json.JsonSerializer.Deserialize<Dictionary<string, object>>(
                    parametersJson, new System.Text.Json.JsonSerializerOptions
                    {
                        PropertyNameCaseInsensitive = true,
                    }) ?? new Dictionary<string, object>(),
            };

            var jsonOptions = new System.Text.Json.JsonSerializerOptions
            {
                PropertyNamingPolicy = System.Text.Json.JsonNamingPolicy.SnakeCaseLower,
                PropertyNameCaseInsensitive = true,
            };

            var httpClient = polygonService.GetHttpClient();
            var response = await httpClient.PostAsJsonAsync(
                "/api/backtest/rule-based/run", requestBody, jsonOptions);

            if (!response.IsSuccessStatusCode)
            {
                var errorBody = await response.Content.ReadAsStringAsync();
                logger.LogError("[RuleBasedBacktest] Python service error: {Status} {Body}",
                    response.StatusCode, errorBody);
                return new RuleBasedBacktestResultType
                {
                    Success = false,
                    Error = $"Python service returned {response.StatusCode}: {errorBody}",
                };
            }

            var result = await response.Content.ReadFromJsonAsync<RuleBasedPythonResponse>(jsonOptions);
            if (result == null)
            {
                return new RuleBasedBacktestResultType { Success = false, Error = "Empty response from Python" };
            }

            return new RuleBasedBacktestResultType
            {
                Success = result.Success,
                Ticker = result.Ticker ?? ticker,
                StrategyName = result.StrategyName ?? "ema_crossover_rsi",
                Parameters = parametersJson,
                TotalTrades = result.TotalTrades,
                WinningTrades = result.WinningTrades,
                LosingTrades = result.LosingTrades,
                WinRate = result.WinRate,
                AvgWinPct = result.AvgWinPct,
                AvgLossPct = result.AvgLossPct,
                WinLossRatio = result.WinLossRatio,
                ProfitFactor = result.ProfitFactor,
                ExpectancyPerTrade = result.ExpectancyPerTrade,
                TotalPnlPct = result.TotalPnlPct,
                MaxDrawdownPct = result.MaxDrawdownPct,
                TotalPnlPts = result.TotalPnlPts,
                SharpeRatio = result.SharpeRatio,
                BarsProcessed = result.BarsProcessed,
                Trades = result.Trades?.Select(t => new RuleBasedTradeType
                {
                    TradeNumber = t.TradeNumber,
                    TradeType = t.TradeType ?? "Buy",
                    EntryTimestamp = t.EntryTimestamp,
                    ExitTimestamp = t.ExitTimestamp,
                    EntryPrice = t.EntryPrice,
                    ExitPrice = t.ExitPrice,
                    Pnl = t.Pnl,
                    PnlPct = t.PnlPct,
                    CumulativePnlPct = t.CumulativePnlPct,
                    SignalReason = t.SignalReason ?? "",
                    EmaFast = t.EmaFast,
                    EmaSlow = t.EmaSlow,
                    EmaGap = t.EmaGap,
                    Rsi = t.Rsi,
                    Adx = t.Adx,
                }).ToList() ?? [],
                Error = result.Error,
            };
        }
        catch (Exception ex)
        {
            logger.LogError(ex, "[RuleBasedBacktest] Error running on {Ticker}", ticker);
            return new RuleBasedBacktestResultType { Success = false, Error = ex.Message };
        }
    }

    #endregion

}

public class BatchResearchResultType
{
    public bool Success { get; set; }
    public string FeatureName { get; set; } = "";
    public int TickersTested { get; set; }
    public int TickersPassed { get; set; }
    public double PassRate { get; set; }
    public bool CrossSectionalConsistent { get; set; }
    public double AggregateIc { get; set; }
    public List<TickerBatchResultType> TickerResults { get; set; } = [];
    public string Summary { get; set; } = "";
    public string? Error { get; set; }
}

public class TickerBatchResultType
{
    public string Ticker { get; set; } = "";
    public double MeanIc { get; set; }
    public double IcTStat { get; set; }
    public double IcPValue { get; set; } = 1.0;
    public double NwTStat { get; set; }
    public double NwPValue { get; set; } = 1.0;
    public double EffectiveN { get; set; }
    public bool IsStationary { get; set; }
    public bool PassedValidation { get; set; }
    public int DataPoints { get; set; }
    public string? Error { get; set; }
}

// Rule-Based Backtest types

public class RuleBasedBacktestResultType
{
    public bool Success { get; set; }
    public string Ticker { get; set; } = "";
    public string StrategyName { get; set; } = "";
    public string Parameters { get; set; } = "{}";
    public int TotalTrades { get; set; }
    public int WinningTrades { get; set; }
    public int LosingTrades { get; set; }
    public double WinRate { get; set; }
    public double AvgWinPct { get; set; }
    public double AvgLossPct { get; set; }
    public double WinLossRatio { get; set; }
    public double ProfitFactor { get; set; }
    public double ExpectancyPerTrade { get; set; }
    public double TotalPnlPct { get; set; }
    public double MaxDrawdownPct { get; set; }
    public double TotalPnlPts { get; set; }
    public double SharpeRatio { get; set; }
    public int BarsProcessed { get; set; }
    public List<RuleBasedTradeType> Trades { get; set; } = [];
    public string? Error { get; set; }
}

public class RuleBasedTradeType
{
    public int TradeNumber { get; set; }
    public string TradeType { get; set; } = "Buy";
    public long EntryTimestamp { get; set; }
    public long ExitTimestamp { get; set; }
    public double EntryPrice { get; set; }
    public double ExitPrice { get; set; }
    [GraphQLName("pnl")]
    public double Pnl { get; set; }
    public double PnlPct { get; set; }
    public double CumulativePnlPct { get; set; }
    public string SignalReason { get; set; } = "";
    public double? EmaFast { get; set; }
    public double? EmaSlow { get; set; }
    public double? EmaGap { get; set; }
    public double? Rsi { get; set; }
    public double? Adx { get; set; }
}

/// <summary>DTO for deserializing the Python rule-based backtest response (snake_case).</summary>
internal class RuleBasedPythonResponse
{
    public bool Success { get; set; }
    public string? Ticker { get; set; }
    public string? StrategyName { get; set; }
    public Dictionary<string, object>? Parameters { get; set; }
    public int TotalTrades { get; set; }
    public int WinningTrades { get; set; }
    public int LosingTrades { get; set; }
    public double WinRate { get; set; }
    public double AvgWinPct { get; set; }
    public double AvgLossPct { get; set; }
    public double WinLossRatio { get; set; }
    public double ProfitFactor { get; set; }
    public double ExpectancyPerTrade { get; set; }
    public double TotalPnlPct { get; set; }
    public double MaxDrawdownPct { get; set; }
    public double TotalPnlPts { get; set; }
    public double SharpeRatio { get; set; }
    public int BarsProcessed { get; set; }
    public List<RuleBasedPythonTrade>? Trades { get; set; }
    public string? Error { get; set; }
}

internal class RuleBasedPythonTrade
{
    public int TradeNumber { get; set; }
    public string? TradeType { get; set; }
    public long EntryTimestamp { get; set; }
    public long ExitTimestamp { get; set; }
    public double EntryPrice { get; set; }
    public double ExitPrice { get; set; }
    public double Pnl { get; set; }
    public double PnlPct { get; set; }
    public double CumulativePnlPct { get; set; }
    public string? SignalReason { get; set; }
    public double? EmaFast { get; set; }
    public double? EmaSlow { get; set; }
    public double? EmaGap { get; set; }
    public double? Rsi { get; set; }
    public double? Adx { get; set; }
}

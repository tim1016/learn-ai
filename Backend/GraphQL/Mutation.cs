using Backend.Data;
using Backend.GraphQL.Types;
using Backend.Models;
using Backend.Models.DTOs;
using Backend.Models.MarketData;
using Backend.Services.Interfaces;
using HotChocolate;
using Microsoft.EntityFrameworkCore;

namespace Backend.GraphQL;

public class Mutation
{
    #region Demo Mutations (Books/Authors)
    public async Task<Author> AddAuthor(
        AppDbContext context,
        string name,
        string? bio)
    {
        var author = new Author { Name = name, Bio = bio };
        context.Authors.Add(author);
        await context.SaveChangesAsync();
        return author;
    }

    public async Task<Book> AddBook(
        AppDbContext context,
        string title,
        int publishedYear,
        int authorId)
    {
        var book = new Book
        {
            Title = title,
            PublishedYear = publishedYear,
            AuthorId = authorId
        };
        context.Books.Add(book);
        await context.SaveChangesAsync();

        await context.Entry(book).Reference(b => b.Author).LoadAsync();
        return book;
    }

    public async Task<Book?> UpdateBook(
        AppDbContext context,
        int id,
        string? title,
        int? publishedYear,
        int? authorId)
    {
        var book = await context.Books.FindAsync(id);
        if (book is null) return null;

        if (title is not null) book.Title = title;
        if (publishedYear.HasValue) book.PublishedYear = publishedYear.Value;
        if (authorId.HasValue) book.AuthorId = authorId.Value;

        await context.SaveChangesAsync();
        await context.Entry(book).Reference(b => b.Author).LoadAsync();
        return book;
    }

    public async Task<bool> DeleteBook(
        AppDbContext context,
        int id)
    {
        var book = await context.Books.FindAsync(id);
        if (book is null) return false;

        context.Books.Remove(book);
        await context.SaveChangesAsync();
        return true;
    }

    #endregion

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

    /// <summary>
    /// Run a backtest with a given strategy on historical data.
    /// Fetches aggregates from DB, runs strategy, persists results.
    /// </summary>
    [GraphQLName("runBacktest")]
    public async Task<BacktestResultType> RunBacktest(
        [Service] IBacktestService backtestService,
        [Service] IMarketDataService marketDataService,
        [Service] ILogger<Mutation> logger,
        AppDbContext context,
        string ticker,
        string strategyName,
        string fromDate,
        string toDate,
        string parametersJson = "{}",
        string timespan = "minute",
        int multiplier = 1)
    {
        try
        {
            logger.LogInformation(
                "[Backtest] Running {Strategy} on {Ticker} from {From} to {To}",
                strategyName, ticker, fromDate, toDate);

            // Get or fetch the aggregates
            var fetchResult = await marketDataService.GetOrFetchAggregatesAsync(
                ticker.ToUpper(), multiplier, timespan, fromDate, toDate, false);
            var aggregates = fetchResult.Aggregates;

            if (aggregates.Count == 0)
            {
                return new BacktestResultType
                {
                    Success = false,
                    Error = $"No aggregates found for {ticker} in date range",
                };
            }

            // Find ticker entity
            var market = ticker.StartsWith("O:", StringComparison.OrdinalIgnoreCase) ? "options" : "stocks";
            var tickerEntity = await context.Tickers
                .FirstOrDefaultAsync(t => t.Symbol == ticker.ToUpper() && t.Market == market);

            if (tickerEntity == null)
            {
                return new BacktestResultType
                {
                    Success = false,
                    Error = $"Ticker {ticker} not found in database",
                };
            }

            var execution = await backtestService.RunBacktestAsync(
                tickerEntity.Id, strategyName, parametersJson,
                fromDate, toDate, timespan, multiplier, aggregates);

            return new BacktestResultType
            {
                Success = true,
                Id = execution.Id,
                StrategyName = execution.StrategyName,
                Parameters = execution.Parameters,
                TotalTrades = execution.TotalTrades,
                WinningTrades = execution.WinningTrades,
                LosingTrades = execution.LosingTrades,
                TotalPnL = execution.TotalPnL,
                MaxDrawdown = execution.MaxDrawdown,
                SharpeRatio = execution.SharpeRatio,
                DurationMs = execution.DurationMs,
                Trades = execution.Trades.Select(t => new BacktestTradeType
                {
                    TradeType = t.TradeType,
                    EntryTimestamp = t.EntryTimestamp.ToString("o"),
                    ExitTimestamp = t.ExitTimestamp.ToString("o"),
                    EntryPrice = t.EntryPrice,
                    ExitPrice = t.ExitPrice,
                    PnL = t.PnL,
                    CumulativePnL = t.CumulativePnL,
                    SignalReason = t.SignalReason,
                }).ToList(),
            };
        }
        catch (Exception ex)
        {
            logger.LogError(ex, "[Backtest] Error running {Strategy} on {Ticker}", strategyName, ticker);
            return new BacktestResultType
            {
                Success = false,
                Error = ex.Message,
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

            return new ResearchResultType
            {
                Success = report.Success,
                Ticker = report.Ticker,
                FeatureName = report.FeatureName,
                StartDate = report.StartDate,
                EndDate = report.EndDate,
                BarsUsed = report.BarsUsed,
                MeanIC = report.MeanIc,
                ICTStat = report.IcTStat,
                ICPValue = report.IcPValue,
                NwTStat = report.NwTStat,
                NwPValue = report.NwPValue,
                EffectiveN = report.EffectiveN,
                ICValues = report.IcValues,
                ICDates = report.IcDates,
                AdfPvalue = report.AdfPvalue,
                KpssPvalue = report.KpssPvalue,
                IsStationary = report.IsStationary,
                QuantileBins = report.QuantileBins.Select(b => new QuantileBinType
                {
                    BinNumber = b.BinNumber,
                    LowerBound = b.LowerBound,
                    UpperBound = b.UpperBound,
                    MeanReturn = b.MeanReturn,
                    Count = b.Count,
                }).ToList(),
                IsMonotonic = report.IsMonotonic,
                MonotonicityRatio = report.MonotonicityRatio,
                PassedValidation = report.PassedValidation,
                Robustness = report.Robustness != null ? new RobustnessType
                {
                    MonthlyBreakdown = report.Robustness.MonthlyBreakdown.Select(m => new MonthlyICBreakdownType
                    {
                        Month = m.Month,
                        MeanIC = m.MeanIc,
                        TStat = m.TStat,
                        ObservationCount = m.ObservationCount,
                    }).ToList(),
                    PctPositiveMonths = report.Robustness.PctPositiveMonths,
                    PctSignificantMonths = report.Robustness.PctSignificantMonths,
                    BestMonthIC = report.Robustness.BestMonthIc,
                    WorstMonthIC = report.Robustness.WorstMonthIc,
                    StabilityLabel = report.Robustness.StabilityLabel,
                    PctSignConsistentMonths = report.Robustness.PctSignConsistentMonths,
                    SignConsistentStabilityLabel = report.Robustness.SignConsistentStabilityLabel,
                    RollingTStat = report.Robustness.RollingTStat.Select(r => new RollingTStatPointType
                    {
                        Month = r.Month,
                        TStatSmoothed = r.TStatSmoothed,
                    }).ToList(),
                    VolatilityRegimes = report.Robustness.VolatilityRegimes.Select(r => new RegimeICType
                    {
                        RegimeLabel = r.RegimeLabel,
                        MeanIC = r.MeanIc,
                        TStat = r.TStat,
                        ObservationCount = r.ObservationCount,
                    }).ToList(),
                    TrendRegimes = report.Robustness.TrendRegimes.Select(r => new RegimeICType
                    {
                        RegimeLabel = r.RegimeLabel,
                        MeanIC = r.MeanIc,
                        TStat = r.TStat,
                        ObservationCount = r.ObservationCount,
                    }).ToList(),
                    TrainTest = report.Robustness.TrainTest != null ? new TrainTestSplitType
                    {
                        TrainStart = report.Robustness.TrainTest.TrainStart,
                        TrainEnd = report.Robustness.TrainTest.TrainEnd,
                        TestStart = report.Robustness.TrainTest.TestStart,
                        TestEnd = report.Robustness.TrainTest.TestEnd,
                        TrainMeanIC = report.Robustness.TrainTest.TrainMeanIc,
                        TrainTStat = report.Robustness.TrainTest.TrainTStat,
                        TrainDays = report.Robustness.TrainTest.TrainDays,
                        TestMeanIC = report.Robustness.TrainTest.TestMeanIc,
                        TestTStat = report.Robustness.TrainTest.TestTStat,
                        TestDays = report.Robustness.TrainTest.TestDays,
                        OverfitFlag = report.Robustness.TrainTest.OverfitFlag,
                        OosRetention = report.Robustness.TrainTest.OosRetention,
                        OosRetentionLabel = report.Robustness.TrainTest.OosRetentionLabel,
                    } : null,
                    StructuralBreaks = report.Robustness.StructuralBreaks.Select(b => new StructuralBreakPointType
                    {
                        Date = b.Date,
                        IcBefore = b.IcBefore,
                        IcAfter = b.IcAfter,
                        TStat = b.TStat,
                        Significant = b.Significant,
                    }).ToList(),
                } : null,
                Error = report.Error,
            };
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

    #endregion

    #region LSTM Prediction Mutations

    [GraphQLName("startLstmTraining")]
    public async Task<LstmJobResult> StartLstmTraining(
        [Service] ILstmService lstmService,
        [Service] ILogger<Mutation> logger,
        string ticker,
        string fromDate,
        string toDate,
        int epochs = 50,
        int sequenceLength = 60,
        string features = "close",
        bool mock = false,
        string scalerType = "standard",
        bool logReturns = false,
        bool winsorize = false,
        string timespan = "day",
        int multiplier = 1)
    {
        try
        {
            logger.LogInformation(
                "[LSTM] Starting training: {Ticker}, epochs={Epochs}, seq={SeqLen}, features={Features}, scaler={Scaler}, timespan={Timespan}",
                ticker, epochs, sequenceLength, features, scalerType, timespan);

            var config = new LstmTrainingConfigDto
            {
                Ticker = ticker,
                FromDate = fromDate,
                ToDate = toDate,
                Epochs = epochs,
                SequenceLength = sequenceLength,
                Features = features,
                Mock = mock,
                ScalerType = scalerType,
                LogReturns = logReturns,
                Winsorize = winsorize,
                Timespan = timespan,
                Multiplier = multiplier,
            };

            var response = await lstmService.StartTrainingAsync(config);

            return new LstmJobResult
            {
                Success = true,
                JobId = response.JobId,
                Message = $"Training job submitted for {ticker}",
            };
        }
        catch (Exception ex)
        {
            logger.LogError(ex, "[LSTM] Error starting training for {Ticker}", ticker);
            return new LstmJobResult
            {
                Success = false,
                Message = ex.Message,
            };
        }
    }

    [GraphQLName("startLstmValidation")]
    public async Task<LstmJobResult> StartLstmValidation(
        [Service] ILstmService lstmService,
        [Service] ILogger<Mutation> logger,
        string ticker,
        string fromDate,
        string toDate,
        int folds = 5,
        int epochs = 20,
        int sequenceLength = 60,
        bool mock = false,
        string scalerType = "standard",
        bool logReturns = false,
        bool winsorize = false,
        string timespan = "day",
        int multiplier = 1)
    {
        try
        {
            logger.LogInformation(
                "[LSTM] Starting validation: {Ticker}, folds={Folds}, epochs={Epochs}, scaler={Scaler}, timespan={Timespan}",
                ticker, folds, epochs, scalerType, timespan);

            var config = new LstmValidationConfigDto
            {
                Ticker = ticker,
                FromDate = fromDate,
                ToDate = toDate,
                Folds = folds,
                Epochs = epochs,
                SequenceLength = sequenceLength,
                Mock = mock,
                ScalerType = scalerType,
                LogReturns = logReturns,
                Winsorize = winsorize,
                Timespan = timespan,
                Multiplier = multiplier,
            };

            var response = await lstmService.StartValidationAsync(config);

            return new LstmJobResult
            {
                Success = true,
                JobId = response.JobId,
                Message = $"Validation job submitted for {ticker}",
            };
        }
        catch (Exception ex)
        {
            logger.LogError(ex, "[LSTM] Error starting validation for {Ticker}", ticker);
            return new LstmJobResult
            {
                Success = false,
                Message = ex.Message,
            };
        }
    }

    #endregion
}

public class BacktestResultType
{
    public bool Success { get; set; }
    public int? Id { get; set; }
    public string? StrategyName { get; set; }
    public string? Parameters { get; set; }
    public int TotalTrades { get; set; }
    public int WinningTrades { get; set; }
    public int LosingTrades { get; set; }
    [GraphQLName("totalPnL")]
    public decimal TotalPnL { get; set; }
    public decimal MaxDrawdown { get; set; }
    public decimal SharpeRatio { get; set; }
    public long DurationMs { get; set; }
    public List<BacktestTradeType> Trades { get; set; } = [];
    public string? Error { get; set; }
}

public class BacktestTradeType
{
    public string TradeType { get; set; } = "";
    public string EntryTimestamp { get; set; } = "";
    public string ExitTimestamp { get; set; } = "";
    public decimal EntryPrice { get; set; }
    public decimal ExitPrice { get; set; }
    [GraphQLName("pnl")]
    public decimal PnL { get; set; }
    [GraphQLName("cumulativePnl")]
    public decimal CumulativePnL { get; set; }
    public string SignalReason { get; set; } = "";
}

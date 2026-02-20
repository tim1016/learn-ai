
using Backend.Data;
using Backend.GraphQL.Types;
using Backend.Models;
using Backend.Models.DTOs;
using Backend.Models.MarketData;
using Backend.Models.DTOs.PolygonResponses;
using Backend.Services.Interfaces;
using HotChocolate;
using Microsoft.EntityFrameworkCore;

namespace Backend.GraphQL;

public class Query
{
    #region Demo Queries (Books/Authors)
    [UseProjection]
    [UseFiltering]
    [UseSorting]
    public IQueryable<Book> GetBooks(AppDbContext context)
        => context.Books;

    [UseProjection]
    [UseFiltering]
    [UseSorting]
    public IQueryable<Author> GetAuthors(AppDbContext context)
        => context.Authors;

    [UseFirstOrDefault]
    [UseProjection]
    public IQueryable<Book?> GetBookById(AppDbContext context, int id)
        => context.Books.Where(b => b.Id == id);

    [UseFirstOrDefault]
    [UseProjection]
    public IQueryable<Author?> GetAuthorById(AppDbContext context, int id)
        => context.Authors.Where(a => a.Id == id);

    #endregion

    #region Market Data Queries

    /// <summary>
    /// Get all tickers
    /// Supports filtering, sorting, and projections
    /// Testable: DbContext injected, can use in-memory DB
    /// </summary>
    [UseProjection]
    [UseFiltering]
    [UseSorting]
    public IQueryable<Ticker> GetTickers(AppDbContext context)
        => context.Tickers;

    /// <summary>
    /// Get stock aggregates with filtering and sorting
    /// Example: Filter by ticker symbol, date range
    /// </summary>
    [UseProjection]
    [UseFiltering]
    [UseSorting]
    public IQueryable<StockAggregate> GetStockAggregates(AppDbContext context)
        => context.StockAggregates;

    /// <summary>
    /// Get trades with filtering
    /// </summary>
    [UseProjection]
    [UseFiltering]
    [UseSorting]
    public IQueryable<Trade> GetTrades(AppDbContext context)
        => context.Trades;

    /// <summary>
    /// Get quotes with filtering
    /// </summary>
    [UseProjection]
    [UseFiltering]
    [UseSorting]
    public IQueryable<Quote> GetQuotes(AppDbContext context)
        => context.Quotes;

    /// <summary>
    /// Get technical indicators
    /// </summary>
    [UseProjection]
    [UseFiltering]
    [UseSorting]
    public IQueryable<TechnicalIndicator> GetTechnicalIndicators(AppDbContext context)
        => context.TechnicalIndicators;

    /// <summary>
    /// Get a specific ticker by symbol
    /// </summary>
    [UseFirstOrDefault]
    [UseProjection]
    public IQueryable<Ticker?> GetTickerBySymbol(AppDbContext context, string symbol)
        => context.Tickers.Where(t => t.Symbol == symbol);

    /// <summary>
    /// Smart query: returns cached data if available, fetches from Polygon if not.
    /// Computes summary statistics server-side.
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
        bool forceRefresh = false)
    {
        logger.LogInformation(
            "[STEP 3 - GraphQL] Query received: ticker={Ticker}, from={From}, to={To}, timespan={Timespan}, multiplier={Multiplier}, forceRefresh={ForceRefresh}",
            ticker, fromDate, toDate, timespan, multiplier, forceRefresh);

        var aggregates = await marketDataService.GetOrFetchAggregatesAsync(
            ticker, multiplier, timespan, fromDate, toDate, forceRefresh);

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
            Timestamp = a.Timestamp,
            Timespan = a.Timespan,
            Multiplier = a.Multiplier,
            TransactionCount = a.TransactionCount
        }).ToList();

        var result = new SmartAggregatesResult
        {
            Ticker = ticker.ToUpper(),
            Aggregates = bars,
            SanitizationSummary = tickerEntity?.SanitizationSummary
        };

        if (bars.Count > 0)
        {
            result.Summary = new AggregatesSummary
            {
                PeriodHigh = bars.Max(a => a.High),
                PeriodLow = bars.Min(a => a.Low),
                AverageVolume = bars.Average(a => a.Volume),
                AverageVwap = bars
                    .Where(a => a.VolumeWeightedAveragePrice.HasValue)
                    .Select(a => a.VolumeWeightedAveragePrice!.Value)
                    .DefaultIfEmpty(0)
                    .Average(),
                OpenPrice = bars.First().Open,
                ClosePrice = bars.Last().Close,
                PriceChange = bars.Last().Close - bars.First().Open,
                PriceChangePercent = bars.First().Open != 0
                    ? (bars.Last().Close - bars.First().Open) / bars.First().Open * 100
                    : 0,
                TotalBars = bars.Count
            };
        }

        logger.LogInformation(
            "[STEP 5 - GraphQL] Returning result: ticker={Ticker}, bars={Bars}, hasSummary={HasSummary}",
            result.Ticker, result.Aggregates.Count, result.Summary != null);

        return result;
    }

    /// <summary>
    /// Calculate technical indicators for a ticker's existing aggregate data.
    /// Reads OHLCV from DB, sends to Python pandas-ta service.
    /// </summary>
    [GraphQLName("calculateIndicators")]
    public async Task<CalculateIndicatorsResult> CalculateIndicators(
        [Service] ITechnicalAnalysisService taService,
        [Service] ILogger<Query> logger,
        AppDbContext context,
        string ticker,
        string fromDate,
        string toDate,
        List<IndicatorConfigInput> indicators,
        string timespan = "day",
        int multiplier = 1)
    {
        try
        {
            var from = DateTime.Parse(fromDate).ToUniversalTime();
            var to = DateTime.Parse(toDate).ToUniversalTime().Date.AddDays(1).AddTicks(-1);
            var symbol = ticker.ToUpper();

            var tickerEntity = await context.Tickers
                .FirstOrDefaultAsync(t => t.Symbol == symbol && t.Market == "stocks");

            if (tickerEntity == null)
            {
                return new CalculateIndicatorsResult
                {
                    Success = false,
                    Ticker = symbol,
                    Message = $"No data found for {symbol}. Fetch market data first."
                };
            }

            var aggregates = await context.StockAggregates
                .Where(a => a.TickerId == tickerEntity.Id
                         && a.Timespan == timespan
                         && a.Multiplier == multiplier
                         && a.Timestamp >= from
                         && a.Timestamp <= to)
                .OrderBy(a => a.Timestamp)
                .ToListAsync();

            if (aggregates.Count == 0)
            {
                return new CalculateIndicatorsResult
                {
                    Success = false,
                    Ticker = symbol,
                    Message = $"No aggregate data in DB for {symbol}. Fetch market data first."
                };
            }

            logger.LogInformation(
                "[TA] Calculating indicators for {Ticker}: {Count} bars, {Indicators} indicators",
                symbol, aggregates.Count, indicators.Count);

            var bars = aggregates.Select(a => new OhlcvBarDto(
                new DateTimeOffset(a.Timestamp, TimeSpan.Zero).ToUnixTimeMilliseconds(),
                a.Open, a.High, a.Low, a.Close, a.Volume
            )).ToList();

            var indicatorConfigs = indicators.Select(i =>
                new IndicatorConfigDto(i.Name, i.Window)).ToList();

            var response = await taService.CalculateIndicatorsAsync(
                symbol, bars, indicatorConfigs);

            return new CalculateIndicatorsResult
            {
                Success = true,
                Ticker = symbol,
                Indicators = response.Indicators.Select(ind => new IndicatorSeriesResult
                {
                    Name = ind.Name,
                    Window = ind.Window,
                    Data = ind.Data.Select(d => new IndicatorPoint
                    {
                        Timestamp = d.Timestamp,
                        Value = d.Value,
                        Signal = d.Signal,
                        Histogram = d.Histogram,
                        Upper = d.Upper,
                        Lower = d.Lower
                    }).ToList()
                }).ToList(),
                Message = $"Calculated {response.Indicators.Count} indicators from {aggregates.Count} bars"
            };
        }
        catch (Exception ex)
        {
            logger.LogError(ex, "[TA] Error calculating indicators for {Ticker}", ticker);
            return new CalculateIndicatorsResult
            {
                Success = false,
                Ticker = ticker.ToUpper(),
                Message = $"Error: {ex.Message}"
            };
        }
    }

    /// <summary>
    /// Check which date ranges already have cached data in the database.
    /// Used by the frontend to show cached vs. uncached chunks before fetching.
    /// </summary>
    [GraphQLName("checkCachedRanges")]
    public async Task<List<CachedRangeResult>> CheckCachedRanges(
        AppDbContext context,
        string ticker,
        List<DateRangeInput> ranges,
        string timespan = "day",
        int multiplier = 1)
    {
        var symbol = ticker.ToUpper();
        var cachedMarket = symbol.StartsWith("O:") ? "options" : "stocks";
        var tickerEntity = await context.Tickers
            .FirstOrDefaultAsync(t => t.Symbol == symbol && t.Market == cachedMarket);

        var results = new List<CachedRangeResult>();

        foreach (var range in ranges)
        {
            var isCached = false;
            if (tickerEntity != null)
            {
                var from = DateTime.Parse(range.FromDate).ToUniversalTime();
                var to = DateTime.Parse(range.ToDate).ToUniversalTime().Date.AddDays(1).AddTicks(-1);

                isCached = await context.StockAggregates.AnyAsync(
                    a => a.TickerId == tickerEntity.Id
                      && a.Timespan == timespan
                      && a.Multiplier == multiplier
                      && a.Timestamp >= from
                      && a.Timestamp <= to);
            }

            results.Add(new CachedRangeResult
            {
                FromDate = range.FromDate,
                ToDate = range.ToDate,
                IsCached = isCached,
            });
        }

        return results;
    }

    /// <summary>
    /// List options contracts from Polygon.io for a given underlying ticker.
    /// Used by frontend to discover ATM ± N strike contracts.
    /// </summary>
    [GraphQLName("getOptionsContracts")]
    public async Task<OptionsContractsResult> GetOptionsContracts(
        [Service] IPolygonService polygonService,
        [Service] ILogger<Query> logger,
        string underlyingTicker,
        string? asOfDate = null,
        string? contractType = null,
        decimal? strikePriceGte = null,
        decimal? strikePriceLte = null,
        string? expirationDate = null,
        string? expirationDateGte = null,
        string? expirationDateLte = null,
        int limit = 100)
    {
        try
        {
            logger.LogInformation(
                "[Options] Query: underlying={Underlying}, asOf={AsOf}, type={Type}, strike=[{Gte},{Lte}]",
                underlyingTicker, asOfDate, contractType, strikePriceGte, strikePriceLte);

            var response = await polygonService.FetchOptionsContractsAsync(
                underlyingTicker, asOfDate, contractType,
                strikePriceGte, strikePriceLte,
                expirationDate, expirationDateGte, expirationDateLte,
                limit);

            var contracts = response.Contracts.Select(c => new OptionsContractResult
            {
                Ticker = c.Ticker,
                UnderlyingTicker = c.UnderlyingTicker,
                ContractType = c.ContractType,
                StrikePrice = c.StrikePrice,
                ExpirationDate = c.ExpirationDate,
                ExerciseStyle = c.ExerciseStyle,
            }).ToList();

            return new OptionsContractsResult
            {
                Success = true,
                Contracts = contracts,
                Count = contracts.Count,
            };
        }
        catch (Exception ex)
        {
            logger.LogError(ex, "[Options] Error fetching contracts for {Underlying}", underlyingTicker);
            return new OptionsContractsResult
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
            }).ToList();

            return new OptionsChainSnapshotResult
            {
                Success = true,
                Underlying = underlying,
                Contracts = contracts,
                Count = contracts.Count,
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

    /// <summary>
    /// Fetch snapshots for multiple stock tickers.
    /// If no tickers provided, returns all available snapshots.
    /// </summary>
    [GraphQLName("getStockSnapshots")]
    public async Task<StockSnapshotsResult> GetStockSnapshots(
        [Service] IPolygonService polygonService,
        [Service] ILogger<Query> logger,
        List<string>? tickers = null)
    {
        try
        {
            logger.LogInformation("[Snapshot] Query: tickers={Tickers}",
                tickers != null ? string.Join(",", tickers) : "all");

            var response = await polygonService.FetchStockSnapshotsAsync(tickers);

            return new StockSnapshotsResult
            {
                Success = response.Success,
                Snapshots = response.Snapshots.Select(MapTickerSnapshot).ToList(),
                Count = response.Count,
                Error = response.Error,
            };
        }
        catch (Exception ex)
        {
            logger.LogError(ex, "[Snapshot] Error fetching stock snapshots");
            return new StockSnapshotsResult { Success = false, Error = ex.Message };
        }
    }

    /// <summary>
    /// Fetch top market movers — gainers or losers.
    /// </summary>
    [GraphQLName("getMarketMovers")]
    public async Task<MarketMoversResult> GetMarketMovers(
        [Service] IPolygonService polygonService,
        [Service] ILogger<Query> logger,
        string direction)
    {
        try
        {
            logger.LogInformation("[Snapshot] Query: movers direction={Direction}", direction);

            var response = await polygonService.FetchMarketMoversAsync(direction);

            return new MarketMoversResult
            {
                Success = response.Success,
                Tickers = response.Tickers.Select(MapTickerSnapshot).ToList(),
                Count = response.Count,
                Error = response.Error,
            };
        }
        catch (Exception ex)
        {
            logger.LogError(ex, "[Snapshot] Error fetching market movers ({Direction})", direction);
            return new MarketMoversResult { Success = false, Error = ex.Message };
        }
    }

    /// <summary>
    /// Fetch unified v3 snapshots with flexible filtering.
    /// </summary>
    [GraphQLName("getUnifiedSnapshot")]
    public async Task<UnifiedSnapshotResult> GetUnifiedSnapshot(
        [Service] IPolygonService polygonService,
        [Service] ILogger<Query> logger,
        List<string>? tickers = null,
        int limit = 10)
    {
        try
        {
            logger.LogInformation("[Snapshot] Query: unified tickers={Tickers}, limit={Limit}",
                tickers != null ? string.Join(",", tickers) : "none", limit);

            var response = await polygonService.FetchUnifiedSnapshotAsync(tickers, limit);

            return new UnifiedSnapshotResult
            {
                Success = response.Success,
                Results = response.Results.Select(r => new UnifiedSnapshotItemResult
                {
                    Ticker = r.Ticker,
                    Type = r.Type,
                    MarketStatus = r.MarketStatus,
                    Name = r.Name,
                    Session = r.Session != null ? new UnifiedSessionResult
                    {
                        Price = r.Session.Price,
                        Change = r.Session.Change,
                        ChangePercent = r.Session.ChangePercent,
                        Open = r.Session.Open,
                        Close = r.Session.Close,
                        High = r.Session.High,
                        Low = r.Session.Low,
                        PreviousClose = r.Session.PreviousClose,
                        Volume = r.Session.Volume,
                    } : null,
                }).ToList(),
                Count = response.Count,
                Error = response.Error,
            };
        }
        catch (Exception ex)
        {
            logger.LogError(ex, "[Snapshot] Error fetching unified snapshots");
            return new UnifiedSnapshotResult { Success = false, Error = ex.Message };
        }
    }

    private static StockTickerSnapshotResult MapTickerSnapshot(
        Backend.Models.DTOs.PolygonResponses.StockTickerSnapshotDto dto) => new()
    {
        Ticker = dto.Ticker,
        Day = dto.Day != null ? new SnapshotBarResult
        {
            Open = dto.Day.Open, High = dto.Day.High, Low = dto.Day.Low,
            Close = dto.Day.Close, Volume = dto.Day.Volume, Vwap = dto.Day.Vwap,
        } : null,
        PrevDay = dto.PrevDay != null ? new SnapshotBarResult
        {
            Open = dto.PrevDay.Open, High = dto.PrevDay.High, Low = dto.PrevDay.Low,
            Close = dto.PrevDay.Close, Volume = dto.PrevDay.Volume, Vwap = dto.PrevDay.Vwap,
        } : null,
        Min = dto.Min != null ? new MinuteBarResult
        {
            Open = dto.Min.Open, High = dto.Min.High, Low = dto.Min.Low,
            Close = dto.Min.Close, Volume = dto.Min.Volume, Vwap = dto.Min.Vwap,
            AccumulatedVolume = dto.Min.AccumulatedVolume, Timestamp = dto.Min.Timestamp,
        } : null,
        TodaysChange = dto.TodaysChange,
        TodaysChangePercent = dto.TodaysChangePercent,
        Updated = dto.Updated,
    };

    #endregion

    #region LSTM Prediction Queries

    [GraphQLName("lstmJobStatus")]
    public async Task<LstmJobStatus> GetLstmJobStatus(
        [Service] ILstmService lstmService,
        [Service] ILogger<Query> logger,
        string jobId)
    {
        try
        {
            logger.LogInformation("[LSTM] Querying job status: {JobId}", jobId);

            var dto = await lstmService.GetJobStatusAsync(jobId);

            var result = new LstmJobStatus
            {
                JobId = dto.JobId,
                Status = dto.Status,
                Error = dto.Error,
                CreatedAt = dto.CreatedAt,
                CompletedAt = dto.CompletedAt,
            };

            if (dto.TrainResult is not null)
            {
                result.TrainResult = new LstmTrainResult
                {
                    Ticker = dto.TrainResult.Ticker,
                    ValRmse = dto.TrainResult.ValRmse,
                    TrainRmse = dto.TrainResult.TrainRmse,
                    BaselineRmse = dto.TrainResult.BaselineRmse,
                    Improvement = dto.TrainResult.Improvement,
                    EpochsCompleted = dto.TrainResult.EpochsCompleted,
                    BestEpoch = dto.TrainResult.BestEpoch,
                    ModelId = dto.TrainResult.ModelId,
                    ActualValues = dto.TrainResult.ActualValues,
                    PredictedValues = dto.TrainResult.PredictedValues,
                    HistoryLoss = dto.TrainResult.HistoryLoss,
                    HistoryValLoss = dto.TrainResult.HistoryValLoss,
                    Residuals = dto.TrainResult.Residuals,
                };
            }

            if (dto.ValidateResult is not null)
            {
                result.ValidateResult = new LstmValidateResult
                {
                    Ticker = dto.ValidateResult.Ticker,
                    NumFolds = dto.ValidateResult.NumFolds,
                    AvgRmse = dto.ValidateResult.AvgRmse,
                    AvgMae = dto.ValidateResult.AvgMae,
                    AvgMape = dto.ValidateResult.AvgMape,
                    AvgDirectionalAccuracy = dto.ValidateResult.AvgDirectionalAccuracy,
                    FoldResults = dto.ValidateResult.FoldResults.Select(f => new LstmFoldResult
                    {
                        Fold = f.Fold,
                        TrainSize = f.TrainSize,
                        TestSize = f.TestSize,
                        Rmse = f.Rmse,
                        Mae = f.Mae,
                        Mape = f.Mape,
                        DirectionalAccuracy = f.DirectionalAccuracy,
                    }).ToList(),
                };
            }

            return result;
        }
        catch (Exception ex)
        {
            logger.LogError(ex, "[LSTM] Error querying job status: {JobId}", jobId);
            return new LstmJobStatus
            {
                JobId = jobId,
                Status = "error",
                Error = ex.Message,
            };
        }
    }

    [GraphQLName("lstmModels")]
    public async Task<List<LstmModelInfo>> GetLstmModels(
        [Service] ILstmService lstmService,
        [Service] ILogger<Query> logger)
    {
        try
        {
            logger.LogInformation("[LSTM] Querying model list");

            var dtos = await lstmService.GetModelsAsync();

            return dtos.Select(d => new LstmModelInfo
            {
                ModelId = d.ModelId,
                Ticker = d.Ticker,
                CreatedAt = d.CreatedAt,
                ValRmse = d.ValRmse,
                TrainRmse = d.TrainRmse,
                BaselineRmse = d.BaselineRmse,
                Improvement = d.Improvement,
                EpochsCompleted = d.EpochsCompleted,
                BestEpoch = d.BestEpoch,
                SequenceLength = d.SequenceLength,
                Features = d.Features,
            }).ToList();
        }
        catch (Exception ex)
        {
            logger.LogError(ex, "[LSTM] Error querying models");
            return [];
        }
    }

    #endregion

    #region Ticker Reference Queries

    /// <summary>
    /// Fetch basic info for a batch of stock tickers from Polygon reference API.
    /// </summary>
    [GraphQLName("getTrackedTickers")]
    public async Task<TrackedTickersResult> GetTrackedTickers(
        [Service] IPolygonService polygonService,
        [Service] ILogger<Query> logger,
        List<string> tickers)
    {
        try
        {
            logger.LogInformation("[Tickers] Query: {Count} tickers requested", tickers.Count);

            var response = await polygonService.FetchTickerListAsync(tickers);

            var items = response.Tickers.Select(t => new TickerInfoResult
            {
                Ticker = t.Ticker,
                Name = t.Name,
                Market = t.Market,
                Type = t.Type,
                Active = t.Active,
                PrimaryExchange = t.PrimaryExchange,
                CurrencyName = t.CurrencyName,
            }).ToList();

            return new TrackedTickersResult
            {
                Success = true,
                Tickers = items,
                Count = items.Count,
            };
        }
        catch (Exception ex)
        {
            logger.LogError(ex, "[Tickers] Error fetching tracked tickers");
            return new TrackedTickersResult { Success = false, Error = ex.Message };
        }
    }

    /// <summary>
    /// Fetch detailed overview for a single ticker from Polygon reference API.
    /// </summary>
    [GraphQLName("getTickerDetails")]
    public async Task<TickerDetailResult> GetTickerDetails(
        [Service] IPolygonService polygonService,
        [Service] ILogger<Query> logger,
        string ticker)
    {
        try
        {
            logger.LogInformation("[Tickers] Details query: {Ticker}", ticker);

            var response = await polygonService.FetchTickerDetailsAsync(ticker);

            return new TickerDetailResult
            {
                Success = response.Success,
                Ticker = response.Ticker,
                Name = response.Name,
                Description = response.Description,
                MarketCap = response.MarketCap,
                HomepageUrl = response.HomepageUrl,
                TotalEmployees = response.TotalEmployees,
                ListDate = response.ListDate,
                SicDescription = response.SicDescription,
                PrimaryExchange = response.PrimaryExchange,
                Type = response.Type,
                WeightedSharesOutstanding = response.WeightedSharesOutstanding,
                Address = response.Address != null ? new TickerAddressResult
                {
                    Address1 = response.Address.Address1,
                    City = response.Address.City,
                    State = response.Address.State,
                    PostalCode = response.Address.PostalCode,
                } : null,
                Error = response.Error,
            };
        }
        catch (Exception ex)
        {
            logger.LogError(ex, "[Tickers] Error fetching details for {Ticker}", ticker);
            return new TickerDetailResult { Success = false, Ticker = ticker, Error = ex.Message };
        }
    }

    /// <summary>
    /// Fetch related company tickers for a given stock from Polygon.
    /// </summary>
    [GraphQLName("getRelatedTickers")]
    public async Task<RelatedTickersResult> GetRelatedTickers(
        [Service] IPolygonService polygonService,
        [Service] ILogger<Query> logger,
        string ticker)
    {
        try
        {
            logger.LogInformation("[Tickers] Related query: {Ticker}", ticker);

            var response = await polygonService.FetchRelatedTickersAsync(ticker);

            return new RelatedTickersResult
            {
                Success = response.Success,
                Ticker = response.Ticker,
                Related = response.Related,
                Error = response.Error,
            };
        }
        catch (Exception ex)
        {
            logger.LogError(ex, "[Tickers] Error fetching related tickers for {Ticker}", ticker);
            return new RelatedTickersResult { Success = false, Ticker = ticker, Error = ex.Message };
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

public class OptionsContractsResult
{
    public bool Success { get; set; }
    public List<OptionsContractResult> Contracts { get; set; } = [];
    public int Count { get; set; }
    public string? Error { get; set; }
}

public class OptionsContractResult
{
    public required string Ticker { get; set; }
    public string? UnderlyingTicker { get; set; }
    public string? ContractType { get; set; }
    public decimal? StrikePrice { get; set; }
    public string? ExpirationDate { get; set; }
    public string? ExerciseStyle { get; set; }
}

public class DateRangeInput
{
    public required string FromDate { get; set; }
    public required string ToDate { get; set; }
}

public class CachedRangeResult
{
    public required string FromDate { get; set; }
    public required string ToDate { get; set; }
    public bool IsCached { get; set; }
}

public class IndicatorConfigInput
{
    public required string Name { get; set; }
    public int Window { get; set; } = 14;
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

public class StockSnapshotsResult
{
    public bool Success { get; set; }
    public List<StockTickerSnapshotResult> Snapshots { get; set; } = [];
    public int Count { get; set; }
    public string? Error { get; set; }
}

public class MarketMoversResult
{
    public bool Success { get; set; }
    public List<StockTickerSnapshotResult> Tickers { get; set; } = [];
    public int Count { get; set; }
    public string? Error { get; set; }
}

public class UnifiedSessionResult
{
    public decimal? Price { get; set; }
    public decimal? Change { get; set; }
    public decimal? ChangePercent { get; set; }
    public decimal? Open { get; set; }
    public decimal? Close { get; set; }
    public decimal? High { get; set; }
    public decimal? Low { get; set; }
    public decimal? PreviousClose { get; set; }
    public decimal? Volume { get; set; }
}

public class UnifiedSnapshotItemResult
{
    public string? Ticker { get; set; }
    public string? Type { get; set; }
    public string? MarketStatus { get; set; }
    public string? Name { get; set; }
    public UnifiedSessionResult? Session { get; set; }
}

public class UnifiedSnapshotResult
{
    public bool Success { get; set; }
    public List<UnifiedSnapshotItemResult> Results { get; set; } = [];
    public int Count { get; set; }
    public string? Error { get; set; }
}

// ------------------------------------------------------------------
// Ticker Reference result types
// ------------------------------------------------------------------

public class TickerInfoResult
{
    public string Ticker { get; set; } = "";
    public string Name { get; set; } = "";
    public string Market { get; set; } = "";
    public string Type { get; set; } = "";
    public bool Active { get; set; }
    public string? PrimaryExchange { get; set; }
    public string? CurrencyName { get; set; }
}

public class TrackedTickersResult
{
    public bool Success { get; set; }
    public List<TickerInfoResult> Tickers { get; set; } = [];
    public int Count { get; set; }
    public string? Error { get; set; }
}

public class TickerAddressResult
{
    public string? Address1 { get; set; }
    public string? City { get; set; }
    public string? State { get; set; }
    public string? PostalCode { get; set; }
}

public class TickerDetailResult
{
    public bool Success { get; set; }
    public string Ticker { get; set; } = "";
    public string Name { get; set; } = "";
    public string? Description { get; set; }
    public double? MarketCap { get; set; }
    public string? HomepageUrl { get; set; }
    public int? TotalEmployees { get; set; }
    public string? ListDate { get; set; }
    public string? SicDescription { get; set; }
    public string? PrimaryExchange { get; set; }
    public string? Type { get; set; }
    public double? WeightedSharesOutstanding { get; set; }
    public TickerAddressResult? Address { get; set; }
    public string? Error { get; set; }
}

public class RelatedTickersResult
{
    public bool Success { get; set; }
    public string Ticker { get; set; } = "";
    public List<string> Related { get; set; } = [];
    public string? Error { get; set; }
}

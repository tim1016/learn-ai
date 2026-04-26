
using Backend.Data;
using Backend.GraphQL.Types;
using Backend.Models.DTOs;
using Backend.Models.MarketData;
using Backend.Models.DTOs.PolygonResponses;
using Backend.Services.Implementation;
using Backend.Services.Interfaces;
using HotChocolate;
using HotChocolate.Data;
using Microsoft.EntityFrameworkCore;

namespace Backend.GraphQL;

public class Query
{
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

    [GraphQLName("getFetchProgress")]
    public FetchProgressInfo? GetFetchProgress(string ticker)
    {
        var progress = MarketDataService.GetProgress(ticker.ToUpper());
        if (progress == null) return null;
        return new FetchProgressInfo
        {
            Ticker = progress.Ticker,
            TotalWindows = progress.TotalWindows,
            CompletedWindows = progress.CompletedWindows,
            BarsFetched = progress.BarsFetched,
            CurrentWindow = progress.CurrentWindow,
            Status = progress.Status,
        };
    }

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
        bool forceRefresh = false,
        bool adjusted = true)
    {
        logger.LogInformation(
            "[STEP 3 - GraphQL] Query received: ticker={Ticker}, from={From}, to={To}, timespan={Timespan}, multiplier={Multiplier}, forceRefresh={ForceRefresh}, adjusted={Adjusted}",
            ticker, fromDate, toDate, timespan, multiplier, forceRefresh, adjusted);

        var fetchResult = await marketDataService.GetOrFetchAggregatesAsync(
            ticker, multiplier, timespan, fromDate, toDate, forceRefresh, adjusted);
        var aggregates = fetchResult.Aggregates;

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
            SanitizationSummary = tickerEntity?.SanitizationSummary,
            GapDetection = fetchResult.GapDetection is { } gap ? new GapDetectionInfo
            {
                TotalWeekdays = gap.TotalWeekdays,
                DaysWithData = gap.DaysWithData,
                MissingDays = gap.MissingDays,
                PartialDays = gap.PartialDays,
                CoveragePercent = gap.CoveragePercent,
                ExpectedBars = gap.ExpectedBars,
                ActualBars = gap.ActualBars,
                MissingDates = gap.MissingDates,
                PartialDates = gap.PartialDates,
            } : null,
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
                    Error = $"No data found for {symbol}. Fetch market data first."
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
                    Error = $"No aggregate data in DB for {symbol}. Fetch market data first."
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
                Success = response.Success,
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
                // Surface Python's error field directly — was dropped when the GraphQL
                // output was named Message while the DTO was Error (audit § 3.3).
                Error = response.Error
            };
        }
        catch (Exception ex)
        {
            logger.LogError(ex, "[TA] Error calculating indicators for {Ticker}", ticker);
            return new CalculateIndicatorsResult
            {
                Success = false,
                Ticker = ticker.ToUpper(),
                Error = ex.Message
            };
        }
    }

    /// <summary>
    /// Generate a full indicator table from Polygon minute data.
    /// Returns OHLCV + EMAs, BB, Supertrend, RSI, MACD, ADX in a tabular format.
    /// </summary>
    [GraphQLName("generateIndicatorTable")]
    public async Task<IndicatorTableResult> GenerateIndicatorTable(
        [Service] ITechnicalAnalysisService taService,
        [Service] ILogger<Query> logger,
        string ticker,
        string fromDate,
        string toDate,
        int multiplier = 1,
        string timespan = "minute",
        List<int>? emaPeriods = null,
        int bbLength = 20,
        double bbStd = 2.0,
        int supertrendLength = 10,
        double supertrendMultiplier = 3.0,
        int rsiLength = 14,
        int rsiMaLength = 14,
        int macdFast = 12,
        int macdSlow = 26,
        int macdSignal = 9,
        int adxLength = 14)
    {
        try
        {
            var request = new IndicatorTableRequestDto(
                Ticker: ticker.ToUpper(),
                FromDate: fromDate,
                ToDate: toDate,
                Multiplier: multiplier,
                Timespan: timespan,
                EmaPeriods: emaPeriods ?? [5, 10, 20, 30, 40, 50, 100, 200],
                BbLength: bbLength,
                BbStd: bbStd,
                SupertrendLength: supertrendLength,
                SupertrendMultiplier: supertrendMultiplier,
                RsiLength: rsiLength,
                RsiMaLength: rsiMaLength,
                MacdFast: macdFast,
                MacdSlow: macdSlow,
                MacdSignal: macdSignal,
                AdxLength: adxLength
            );

            var response = await taService.GenerateIndicatorTableAsync(request);

            // Serialize each row dict to JSON string for GraphQL transport
            var jsonRows = response.Rows
                .Select(row => System.Text.Json.JsonSerializer.Serialize(row))
                .ToList();

            return new IndicatorTableResult
            {
                Success = true,
                Ticker = ticker.ToUpper(),
                RowCount = response.RowCount,
                Columns = response.Columns,
                Rows = jsonRows,
            };
        }
        catch (Exception ex)
        {
            logger.LogError(ex, "[TA-TABLE] Error generating indicator table for {Ticker}", ticker);
            return new IndicatorTableResult
            {
                Success = false,
                Ticker = ticker.ToUpper(),
                Error = ex.Message,
            };
        }
    }

    /// <summary>
    /// List all available pandas-ta indicators grouped by category.
    /// </summary>
    [GraphQLName("availableIndicators")]
    public async Task<AvailableIndicatorsResult> AvailableIndicators(
        [Service] ITechnicalAnalysisService taService,
        [Service] ILogger<Query> logger)
    {
        try
        {
            var response = await taService.GetAvailableIndicatorsAsync();

            var categories = response.Categories.Select(kv => new IndicatorCategory
            {
                Name = kv.Key,
                Indicators = kv.Value.Select(i => new IndicatorInfoItem
                {
                    Name = i.Name,
                    Category = i.Category,
                    Description = i.Description,
                }).ToList(),
            }).ToList();

            return new AvailableIndicatorsResult
            {
                Success = true,
                Categories = categories,
                Total = response.Total,
            };
        }
        catch (Exception ex)
        {
            logger.LogError(ex, "Error fetching available indicators");
            return new AvailableIndicatorsResult
            {
                Success = false,
                Error = ex.Message,
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

    [GraphQLName("getResearchExperiment")]
    public async Task<ResearchExperimentType?> GetResearchExperiment(
        [Service] IResearchService researchService,
        int id)
    {
        var experiment = await researchService.GetExperimentAsync(id);
        if (experiment is null) return null;

        return new ResearchExperimentType
        {
            Id = experiment.Id,
            Ticker = experiment.Ticker,
            FeatureName = experiment.FeatureName,
            StartDate = experiment.StartDate,
            EndDate = experiment.EndDate,
            BarsUsed = experiment.BarsUsed,
            MeanIC = experiment.MeanIC,
            ICTStat = experiment.ICTStat,
            ICPValue = experiment.ICPValue,
            AdfPValue = experiment.AdfPValue,
            KpssPValue = experiment.KpssPValue,
            IsStationary = experiment.IsStationary,
            PassedValidation = experiment.PassedValidation,
            MonotonicityRatio = experiment.MonotonicityRatio,
            IsMonotonic = experiment.IsMonotonic,
            CreatedAt = experiment.CreatedAt,
        };
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
    /// Check QuantLib availability and list supported pricing engines.
    /// </summary>
    [GraphQLName("quantlibStatus")]
    public async Task<QuantLibStatusResult> QuantLibStatus(
        [Service] IPolygonService polygonService,
        [Service] ILogger<Query> logger)
    {
        try
        {
            var response = await polygonService.QuantLibStatusAsync();
            return new QuantLibStatusResult
            {
                Available = response.Available,
                Version = response.Version,
                Engines = response.Engines,
            };
        }
        catch (Exception ex)
        {
            logger.LogError(ex, "[QuantLib] Error checking status");
            return new QuantLibStatusResult { Available = false };
        }
    }

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

    /// <summary>
    /// Price a multi-leg strategy via QuantLib for validation.
    /// </summary>
    [GraphQLName("quantlibStrategy")]
    public async Task<QuantLibStrategyResult> QuantLibStrategy(
        [Service] IPolygonService polygonService,
        [Service] ILogger<Query> logger,
        decimal spot,
        List<StrategyLegInput> legs,
        string expirationDate,
        decimal riskFreeRate = 0.05m,
        string? evaluationDate = null,
        decimal dividendYield = 0m,
        string engine = "analytic_bs")
    {
        try
        {
            var response = await polygonService.QuantLibStrategyAsync(
                spot, legs, expirationDate, riskFreeRate,
                evaluationDate, dividendYield, engine);

            return new QuantLibStrategyResult
            {
                Success = response.Success,
                Engine = response.Engine,
                NetPrice = response.NetPrice,
                NetDelta = response.NetDelta,
                NetGamma = response.NetGamma,
                NetTheta = response.NetTheta,
                NetVega = response.NetVega,
                NetRho = response.NetRho,
                Legs = response.Legs.Select(l => new QuantLibLegResultGql
                {
                    Engine = l.Engine,
                    Price = l.Price,
                    Delta = l.Delta,
                    Gamma = l.Gamma,
                    Theta = l.Theta,
                    Vega = l.Vega,
                    Rho = l.Rho,
                    D1 = l.D1,
                    D2 = l.D2,
                }).ToList(),
                Error = response.Error,
            };
        }
        catch (Exception ex)
        {
            logger.LogError(ex, "[QuantLib] Error pricing strategy");
            return new QuantLibStrategyResult { Success = false, Error = ex.Message };
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

public class OptionsExpirationsResult
{
    public bool Success { get; set; }
    public List<string> Expirations { get; set; } = [];
    public int Count { get; set; }
    public string? Error { get; set; }
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
    // request flag was false (the default).
    public List<CurrentCurvePointResult>? CurrentCurve { get; set; }
    public List<GreekCurvePointResult>? GreekCurves { get; set; }
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
    public decimal LegPnl { get; set; }
}

// ------------------------------------------------------------------
// QuantLib Validation result types
// ------------------------------------------------------------------

public class QuantLibStatusResult
{
    public bool Available { get; set; }
    public string? Version { get; set; }
    public List<string> Engines { get; set; } = [];
}

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

public class QuantLibLegResultGql
{
    public string Engine { get; set; } = "";
    public decimal Price { get; set; }
    public decimal Delta { get; set; }
    public decimal Gamma { get; set; }
    public decimal Theta { get; set; }
    public decimal Vega { get; set; }
    public decimal Rho { get; set; }
    public decimal? D1 { get; set; }
    public decimal? D2 { get; set; }
}

public class QuantLibStrategyResult
{
    public bool Success { get; set; }
    public string Engine { get; set; } = "";
    public decimal NetPrice { get; set; }
    public decimal NetDelta { get; set; }
    public decimal NetGamma { get; set; }
    public decimal NetTheta { get; set; }
    public decimal NetVega { get; set; }
    public decimal NetRho { get; set; }
    public List<QuantLibLegResultGql> Legs { get; set; } = [];
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

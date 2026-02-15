
using Backend.Data;
using Backend.GraphQL.Types;
using Backend.Models;
using Backend.Models.DTOs;
using Backend.Models.MarketData;
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
        var tickerEntity = await context.Tickers
            .FirstOrDefaultAsync(t => t.Symbol == ticker.ToUpper() && t.Market == "stocks");

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
        var tickerEntity = await context.Tickers
            .FirstOrDefaultAsync(t => t.Symbol == symbol && t.Market == "stocks");

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

    #endregion
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

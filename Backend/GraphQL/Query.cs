
using Backend.Data;
using Backend.GraphQL.Types;
using Backend.Models;
using Backend.Models.MarketData;
using Backend.Services.Interfaces;
using HotChocolate;

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
    public async Task<SmartAggregatesResult> GetOrFetchStockAggregates(
        [Service] IMarketDataService marketDataService,
        string ticker,
        string fromDate,
        string toDate,
        string timespan = "day",
        int multiplier = 1)
    {
        var aggregates = await marketDataService.GetOrFetchAggregatesAsync(
            ticker, multiplier, timespan, fromDate, toDate);

        var result = new SmartAggregatesResult { Ticker = ticker.ToUpper(), Aggregates = aggregates };

        if (aggregates.Count > 0)
        {
            result.Summary = new AggregatesSummary
            {
                PeriodHigh = aggregates.Max(a => a.High),
                PeriodLow = aggregates.Min(a => a.Low),
                AverageVolume = aggregates.Average(a => a.Volume),
                AverageVwap = aggregates
                    .Where(a => a.VolumeWeightedAveragePrice.HasValue)
                    .Select(a => a.VolumeWeightedAveragePrice!.Value)
                    .DefaultIfEmpty(0)
                    .Average(),
                OpenPrice = aggregates.First().Open,
                ClosePrice = aggregates.Last().Close,
                PriceChange = aggregates.Last().Close - aggregates.First().Open,
                PriceChangePercent = aggregates.First().Open != 0
                    ? (aggregates.Last().Close - aggregates.First().Open) / aggregates.First().Open * 100
                    : 0,
                TotalBars = aggregates.Count
            };
        }

        return result;
    }

    #endregion
}

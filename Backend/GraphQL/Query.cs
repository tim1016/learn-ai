
using Backend.Data;
using Backend.Models;
using Backend.Models.MarketData;

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

    #endregion
}

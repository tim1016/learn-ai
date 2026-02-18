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
            var aggregates = await marketDataService.GetOrFetchAggregatesAsync(
                ticker.ToUpper(), multiplier, timespan, fromDate, toDate, false);

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

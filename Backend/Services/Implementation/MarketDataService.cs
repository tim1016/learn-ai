using Backend.Data;
using Backend.Models.MarketData;
using Backend.Services.Interfaces;
using Microsoft.EntityFrameworkCore;

namespace Backend.Services.Implementation;

/// <summary>
/// Orchestrates market data fetching and persistence
/// Testable: Dependencies injected via interfaces, business logic isolated
/// </summary>
public class MarketDataService : IMarketDataService
{
    private readonly AppDbContext _context;
    private readonly IPolygonService _polygonService;
    private readonly ILogger<MarketDataService> _logger;

    public MarketDataService(
        AppDbContext context,
        IPolygonService polygonService,
        ILogger<MarketDataService> logger)
    {
        _context = context ?? throw new ArgumentNullException(nameof(context));
        _polygonService = polygonService ?? throw new ArgumentNullException(nameof(polygonService));
        _logger = logger ?? throw new ArgumentNullException(nameof(logger));
    }

    public async Task<List<StockAggregate>> FetchAndStoreAggregatesAsync(
        string ticker,
        int multiplier,
        string timespan,
        string fromDate,
        string toDate,
        CancellationToken cancellationToken = default)
    {
        try
        {
            // Get or create ticker
            var tickerEntity = await GetOrCreateTickerAsync(ticker, "stocks", cancellationToken);

            // Fetch sanitized data from Python service
            var response = await _polygonService.FetchAggregatesAsync(
                ticker, multiplier, timespan, fromDate, toDate, cancellationToken);

            _logger.LogInformation(
                "[STEP 9 - MarketDataService] FetchAndStore: Python response success={Success}, dataCount={Count}",
                response.Success, response.Data?.Count ?? 0);

            if (response.Data == null || response.Data.Count == 0)
            {
                _logger.LogWarning("[STEP 9 - MarketDataService] DATA LOST HERE — No data returned for {Ticker}", ticker);
                return [];
            }

            // Save sanitization summary on the ticker
            if (response.Summary is { } summary)
            {
                tickerEntity.SanitizationSummary =
                    $"{summary.CleanedCount} records: {summary.OriginalCount} → {summary.CleanedCount} " +
                    $"({summary.RemovedCount} removed, {summary.RemovalPercentage ?? 0}%). " +
                    $"Fix_DQ: outliers clipped at 0.99 quantile.";
                tickerEntity.UpdatedAt = DateTime.UtcNow;
            }

            // Convert DTOs to entities (mapping logic testable)
            var aggregates = response.Data.Select(d => MapToEntity(d, tickerEntity.Id, timespan, multiplier)).ToList();

            // Upsert logic (testable business rule)
            var storedAggregates = await UpsertAggregatesAsync(aggregates, cancellationToken);

            await _context.SaveChangesAsync(cancellationToken);

            _logger.LogInformation(
                "Stored {Count} aggregates for {Ticker}",
                storedAggregates.Count, ticker);

            return storedAggregates;
        }
        catch (Exception ex)
        {
            _logger.LogError(ex, "Error fetching and storing aggregates for {Ticker}", ticker);
            throw;
        }
    }

    public async Task<List<StockAggregate>> GetOrFetchAggregatesAsync(
        string ticker,
        int multiplier,
        string timespan,
        string fromDate,
        string toDate,
        bool forceRefresh = false,
        CancellationToken cancellationToken = default)
    {
        var from = DateTime.Parse(fromDate).ToUniversalTime();
        var to = DateTime.Parse(toDate).ToUniversalTime().Date.AddDays(1).AddTicks(-1);
        var symbol = ticker.ToUpper();

        if (forceRefresh)
        {
            _logger.LogInformation(
                "[MarketDataService] FORCE REFRESH for {Ticker} from {From} to {To}, bypassing cache",
                symbol, fromDate, toDate);

            return await FetchAndStoreAggregatesAsync(
                ticker, multiplier, timespan, fromDate, toDate, cancellationToken);
        }

        var tickerEntity = await _context.Tickers
            .FirstOrDefaultAsync(t => t.Symbol == symbol && t.Market == "stocks", cancellationToken);

        _logger.LogInformation(
            "[STEP 4.5 - MarketDataService] GetOrFetch: ticker={Ticker}, from={From}, to={To}, tickerExists={Exists}",
            symbol, from, to, tickerEntity != null);

        if (tickerEntity != null)
        {
            var existing = await _context.StockAggregates
                .Where(a => a.TickerId == tickerEntity.Id
                         && a.Timespan == timespan
                         && a.Multiplier == multiplier
                         && a.Timestamp >= from
                         && a.Timestamp <= to)
                .OrderBy(a => a.Timestamp)
                .ToListAsync(cancellationToken);

            _logger.LogInformation(
                "[STEP 4.7 - MarketDataService] DB query returned {Count} cached aggregates for {Ticker}",
                existing.Count, symbol);

            if (existing.Count > 0)
            {
                _logger.LogInformation(
                    "[STEP 4.7 - MarketDataService] CACHE HIT: {Count} aggregates for {Ticker} from {From} to {To}",
                    existing.Count, symbol, fromDate, toDate);
                return existing;
            }
        }

        // Cache miss — fetch from Polygon and store
        _logger.LogInformation(
            "[STEP 5.5 - MarketDataService] CACHE MISS for {Ticker} from {From} to {To}, fetching from Polygon",
            symbol, fromDate, toDate);

        return await FetchAndStoreAggregatesAsync(
            ticker, multiplier, timespan, fromDate, toDate, cancellationToken);
    }

    public async Task<Ticker> GetOrCreateTickerAsync(
        string symbol,
        string market,
        CancellationToken cancellationToken = default)
    {
        var ticker = await _context.Tickers
            .FirstOrDefaultAsync(
                t => t.Symbol == symbol && t.Market == market,
                cancellationToken);

        if (ticker == null)
        {
            ticker = new Ticker
            {
                Symbol = symbol,
                Name = symbol, // TODO: Fetch from reference data endpoint
                Market = market,
                Active = true,
                CreatedAt = DateTime.UtcNow
            };

            _context.Tickers.Add(ticker);
            await _context.SaveChangesAsync(cancellationToken);

            _logger.LogInformation("Created new ticker: {Symbol} ({Market})", symbol, market);
        }

        return ticker;
    }

    /// <summary>
    /// Maps DTO to entity (testable pure function)
    /// </summary>
    private static StockAggregate MapToEntity(
        Models.DTOs.PolygonResponses.AggregateData dto,
        int tickerId,
        string timespan,
        int multiplier)
    {
        return new StockAggregate
        {
            TickerId = tickerId,
            Open = dto.Open,
            High = dto.High,
            Low = dto.Low,
            Close = dto.Close,
            Volume = dto.Volume,
            VolumeWeightedAveragePrice = dto.Vwap,
            Timestamp = DateTime.Parse(dto.Timestamp).ToUniversalTime(),
            Timespan = timespan,
            Multiplier = multiplier,
            TransactionCount = dto.Transactions,
            CreatedAt = DateTime.UtcNow
        };
    }

    /// <summary>
    /// Upsert aggregates (testable business logic)
    /// Checks for existing records, updates or inserts
    /// </summary>
    private async Task<List<StockAggregate>> UpsertAggregatesAsync(
        List<StockAggregate> aggregates,
        CancellationToken cancellationToken)
    {
        var result = new List<StockAggregate>();

        foreach (var agg in aggregates)
        {
            var existing = await _context.StockAggregates
                .FirstOrDefaultAsync(
                    a => a.TickerId == agg.TickerId &&
                         a.Timestamp == agg.Timestamp &&
                         a.Timespan == agg.Timespan &&
                         a.Multiplier == agg.Multiplier,
                    cancellationToken);

            if (existing == null)
            {
                // Insert new
                _context.StockAggregates.Add(agg);
                result.Add(agg);
            }
            else
            {
                // Update existing
                existing.Open = agg.Open;
                existing.High = agg.High;
                existing.Low = agg.Low;
                existing.Close = agg.Close;
                existing.Volume = agg.Volume;
                existing.VolumeWeightedAveragePrice = agg.VolumeWeightedAveragePrice;
                existing.TransactionCount = agg.TransactionCount;
                result.Add(existing);
            }
        }

        return result;
    }
}

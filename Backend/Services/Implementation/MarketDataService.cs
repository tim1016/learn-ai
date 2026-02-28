using System.Collections.Concurrent;
using Backend.Data;
using Backend.Models.DTOs;
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
    private static readonly ConcurrentDictionary<string, FetchProgress> _progressTracker = new();

    public static FetchProgress? GetProgress(string ticker)
    {
        _progressTracker.TryGetValue(ticker.ToUpper(), out var progress);
        return progress;
    }

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

    private static string DetectMarket(string ticker) =>
        ticker.StartsWith("O:", StringComparison.OrdinalIgnoreCase) ? "options" : "stocks";

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
            // Get or create ticker — detect market from O: prefix
            var market = DetectMarket(ticker);
            var tickerEntity = await GetOrCreateTickerAsync(ticker, market, cancellationToken);

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

    public async Task<AggregatesWithGapInfo> GetOrFetchAggregatesAsync(
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

            var (fetchedAggs, windowStatuses) = await FetchWithWindowsAsync(
                ticker, multiplier, timespan, fromDate, toDate, cancellationToken);

            var gapInfo = DetectGaps(fetchedAggs, fromDate, toDate, timespan, multiplier);
            gapInfo.WindowStatuses = windowStatuses;

            return new AggregatesWithGapInfo
            {
                Aggregates = fetchedAggs,
                GapDetection = gapInfo
            };
        }

        var market = DetectMarket(symbol);
        var tickerEntity = await _context.Tickers
            .FirstOrDefaultAsync(t => t.Symbol == symbol && t.Market == market, cancellationToken);

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

                var gapInfo = DetectGaps(existing, fromDate, toDate, timespan, multiplier);
                return new AggregatesWithGapInfo
                {
                    Aggregates = existing,
                    GapDetection = gapInfo
                };
            }
        }

        // Cache miss — fetch from Polygon using windowed approach
        _logger.LogInformation(
            "[STEP 5.5 - MarketDataService] CACHE MISS for {Ticker} from {From} to {To}, fetching from Polygon",
            symbol, fromDate, toDate);

        var (aggs, statuses) = await FetchWithWindowsAsync(
            ticker, multiplier, timespan, fromDate, toDate, cancellationToken);

        var gap = DetectGaps(aggs, fromDate, toDate, timespan, multiplier);
        gap.WindowStatuses = statuses;

        return new AggregatesWithGapInfo
        {
            Aggregates = aggs,
            GapDetection = gap
        };
    }

    #region Windowed Fetch

    internal static List<(string FromDate, string ToDate)> GenerateFetchWindows(
        string fromDate, string toDate, string timespan)
    {
        var from = DateTime.Parse(fromDate);
        var to = DateTime.Parse(toDate);

        // Only split for minute and hour timespans
        if (timespan is not ("minute" or "hour"))
            return [(fromDate, toDate)];

        var windowMonths = timespan == "minute" ? 1 : 3;
        var windows = new List<(string, string)>();
        var cursor = from;

        while (cursor <= to)
        {
            var windowEnd = cursor.AddMonths(windowMonths).AddDays(-1);
            if (windowEnd > to) windowEnd = to;

            windows.Add((cursor.ToString("yyyy-MM-dd"), windowEnd.ToString("yyyy-MM-dd")));
            cursor = windowEnd.AddDays(1);
        }

        return windows;
    }

    private async Task<(List<StockAggregate> Aggregates, List<WindowFetchStatus> Statuses)> FetchWithWindowsAsync(
        string ticker,
        int multiplier,
        string timespan,
        string fromDate,
        string toDate,
        CancellationToken cancellationToken)
    {
        var windows = GenerateFetchWindows(fromDate, toDate, timespan);
        var key = ticker.ToUpper();

        _logger.LogInformation(
            "[STEP W1] Windowed fetch for {Ticker}: {WindowCount} windows from {From} to {To}",
            ticker, windows.Count, fromDate, toDate);

        var progress = new FetchProgress
        {
            Ticker = key,
            TotalWindows = windows.Count,
            Status = "fetching"
        };
        _progressTracker[key] = progress;

        var allAggregates = new List<StockAggregate>();
        var statuses = new List<WindowFetchStatus>();

        try
        {
            for (var i = 0; i < windows.Count; i++)
            {
                var (winFrom, winTo) = windows[i];
                progress.CurrentWindow = $"{winFrom} to {winTo}";

                _logger.LogInformation(
                    "[STEP W2] Window {Index}/{Total}: {From} to {To}",
                    i + 1, windows.Count, winFrom, winTo);

                try
                {
                    var windowAggs = await FetchAndStoreAggregatesAsync(
                        ticker, multiplier, timespan, winFrom, winTo, cancellationToken);

                    _logger.LogInformation(
                        "[STEP W3] Window {Index}/{Total} result: {Count} bars fetched",
                        i + 1, windows.Count, windowAggs.Count);

                    allAggregates.AddRange(windowAggs);
                    progress.CompletedWindows = i + 1;
                    progress.BarsFetched += windowAggs.Count;

                    statuses.Add(new WindowFetchStatus
                    {
                        FromDate = winFrom,
                        ToDate = winTo,
                        Success = true,
                        BarsFetched = windowAggs.Count,
                    });
                }
                catch (Exception ex)
                {
                    _logger.LogWarning(ex,
                        "[STEP W3] Window {Index}/{Total} FAILED: {From} to {To}",
                        i + 1, windows.Count, winFrom, winTo);

                    progress.CompletedWindows = i + 1;

                    statuses.Add(new WindowFetchStatus
                    {
                        FromDate = winFrom,
                        ToDate = winTo,
                        Success = false,
                        BarsFetched = 0,
                        Error = ex.Message,
                    });
                }
            }

            progress.Status = "done";
        }
        catch
        {
            progress.Status = "error";
            throw;
        }
        finally
        {
            // Remove progress after a brief delay so the frontend can read the final state
            _ = Task.Delay(TimeSpan.FromSeconds(5)).ContinueWith(t => _progressTracker.TryRemove(key, out var removed));
        }

        // Sort by timestamp to ensure contiguous ordering
        allAggregates.Sort((a, b) => a.Timestamp.CompareTo(b.Timestamp));

        return (allAggregates, statuses);
    }

    #endregion

    #region Gap Detection

    internal static GapDetectionResult DetectGaps(
        List<StockAggregate> aggregates,
        string fromDate,
        string toDate,
        string timespan,
        int multiplier)
    {
        var from = DateTime.Parse(fromDate);
        var to = DateTime.Parse(toDate);

        // Count weekdays in the range
        var totalWeekdays = 0;
        for (var d = from; d <= to; d = d.AddDays(1))
        {
            if (d.DayOfWeek is not (DayOfWeek.Saturday or DayOfWeek.Sunday))
                totalWeekdays++;
        }

        // Group bars by date
        var barsByDate = aggregates
            .GroupBy(a => a.Timestamp.Date)
            .ToDictionary(g => g.Key, g => g.Count());

        // Expected bars per day based on timespan
        var expectedBarsPerDay = timespan switch
        {
            "minute" => 390 / multiplier,  // 6.5 hours * 60 minutes
            "hour" => 7 / multiplier,       // ~7 trading hours
            _ => 1
        };
        if (expectedBarsPerDay < 1) expectedBarsPerDay = 1;

        var missingDates = new List<string>();
        var partialDates = new List<string>();
        var daysWithData = 0;
        var partialThreshold = expectedBarsPerDay * 0.5;

        for (var d = from; d <= to; d = d.AddDays(1))
        {
            if (d.DayOfWeek is DayOfWeek.Saturday or DayOfWeek.Sunday)
                continue;

            if (barsByDate.TryGetValue(d.Date, out var count))
            {
                daysWithData++;

                // Only flag partial days for intraday timespans
                if (timespan is "minute" or "hour" && count < partialThreshold)
                    partialDates.Add(d.ToString("yyyy-MM-dd"));
            }
            else
            {
                missingDates.Add(d.ToString("yyyy-MM-dd"));
            }
        }

        var coveragePercent = totalWeekdays > 0
            ? Math.Round((decimal)daysWithData / totalWeekdays * 100, 1)
            : 0;

        return new GapDetectionResult
        {
            TotalWeekdays = totalWeekdays,
            DaysWithData = daysWithData,
            MissingDays = missingDates.Count,
            PartialDays = partialDates.Count,
            CoveragePercent = coveragePercent,
            ExpectedBars = totalWeekdays * expectedBarsPerDay,
            ActualBars = aggregates.Count,
            MissingDates = missingDates,
            PartialDates = partialDates,
        };
    }

    #endregion

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
    /// Upsert aggregates using batch operations (fixes N+1 query problem)
    /// Fetches existing records once, then applies updates in batch
    /// </summary>
    private async Task<List<StockAggregate>> UpsertAggregatesAsync(
        List<StockAggregate> aggregates,
        CancellationToken cancellationToken)
    {
        if (aggregates.Count == 0)
            return [];

        // Extract filter values for a translatable SQL query
        var tickerIds = aggregates.Select(a => a.TickerId).Distinct().ToList();
        var timespans = aggregates.Select(a => a.Timespan).Distinct().ToList();
        var multipliers = aggregates.Select(a => a.Multiplier).Distinct().ToList();
        var minTimestamp = aggregates.Min(a => a.Timestamp);
        var maxTimestamp = aggregates.Max(a => a.Timestamp);

        // Fetch candidate records using simple, translatable WHERE clauses
        var existingRecords = await _context.StockAggregates
            .Where(a => tickerIds.Contains(a.TickerId)
                && timespans.Contains(a.Timespan)
                && multipliers.Contains(a.Multiplier)
                && a.Timestamp >= minTimestamp
                && a.Timestamp <= maxTimestamp)
            .ToListAsync(cancellationToken);

        // Build lookup for O(1) access
        var existingLookup = existingRecords
            .ToDictionary(a => (a.TickerId, a.Timestamp, a.Timespan, a.Multiplier));

        var result = new List<StockAggregate>();

        // Process each aggregate, updating or inserting as needed
        foreach (var agg in aggregates)
        {
            var key = (agg.TickerId, agg.Timestamp, agg.Timespan, agg.Multiplier);

            if (existingLookup.TryGetValue(key, out var existing))
            {
                // Update existing record
                existing.Open = agg.Open;
                existing.High = agg.High;
                existing.Low = agg.Low;
                existing.Close = agg.Close;
                existing.Volume = agg.Volume;
                existing.VolumeWeightedAveragePrice = agg.VolumeWeightedAveragePrice;
                existing.TransactionCount = agg.TransactionCount;
                result.Add(existing);
            }
            else
            {
                // Insert new record
                _context.StockAggregates.Add(agg);
                result.Add(agg);
            }
        }

        return result;
    }
}

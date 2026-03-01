using System.Net.Http.Json;
using System.Text.Json;
using Backend.Data;
using Backend.Models.DTOs;
using Backend.Models.MarketData;
using Backend.Services.Interfaces;
using Microsoft.EntityFrameworkCore;

namespace Backend.Services.Implementation;

public class ResearchService : IResearchService
{
    private readonly HttpClient _httpClient;
    private readonly ILogger<ResearchService> _logger;
    private readonly AppDbContext _context;
    private readonly IMarketDataService _marketDataService;

    private static readonly JsonSerializerOptions _jsonOptions = new()
    {
        PropertyNamingPolicy = JsonNamingPolicy.SnakeCaseLower
    };

    public ResearchService(
        HttpClient httpClient,
        ILogger<ResearchService> logger,
        AppDbContext context,
        IMarketDataService marketDataService)
    {
        _httpClient = httpClient;
        _logger = logger;
        _context = context;
        _marketDataService = marketDataService;
    }

    public async Task<ResearchReportDto> RunFeatureResearchAsync(
        string ticker,
        string featureName,
        string fromDate,
        string toDate,
        string timespan = "minute",
        int multiplier = 1,
        CancellationToken cancellationToken = default)
    {
        _logger.LogInformation(
            "[Research] Running {Feature} on {Ticker} from {From} to {To}",
            featureName, ticker, fromDate, toDate);

        // Step 1: Fetch cached aggregates from PostgreSQL
        var fetchResult = await _marketDataService.GetOrFetchAggregatesAsync(
            ticker.ToUpper(), multiplier, timespan, fromDate, toDate,
            forceRefresh: false, cancellationToken: cancellationToken);
        var aggregates = fetchResult.Aggregates;

        if (aggregates.Count == 0)
        {
            return new ResearchReportDto
            {
                Success = false,
                Ticker = ticker,
                FeatureName = featureName,
                Error = $"No aggregates found for {ticker} in date range {fromDate} to {toDate}"
            };
        }

        _logger.LogInformation(
            "[Research] Found {Count} aggregates for {Ticker}, sending to Python",
            aggregates.Count, ticker);

        // Step 2: Convert to OHLCV bars for Python
        var bars = aggregates.Select(a => new OhlcvBarDto(
            new DateTimeOffset(a.Timestamp, TimeSpan.Zero).ToUnixTimeMilliseconds(),
            a.Open, a.High, a.Low, a.Close, a.Volume
        )).ToList();

        var request = new RunFeatureResearchRequest
        {
            Ticker = ticker.ToUpper(),
            FeatureName = featureName,
            Bars = bars,
            StartDate = fromDate,
            EndDate = toDate,
        };

        // Step 3: POST to Python research endpoint
        var response = await _httpClient.PostAsJsonAsync(
            "/api/research/run-feature", request, _jsonOptions, cancellationToken);

        response.EnsureSuccessStatusCode();

        var report = await response.Content.ReadFromJsonAsync<ResearchReportDto>(
            _jsonOptions, cancellationToken);

        if (report is null)
            throw new HttpRequestException("Failed to parse research report from Python service");

        _logger.LogInformation(
            "[Research] Completed {Feature} on {Ticker}: IC={IC:F4}, passed={Passed}",
            featureName, ticker, report.MeanIc, report.PassedValidation);

        // Step 4: Persist experiment to database
        if (report.Success)
        {
            await PersistExperimentAsync(ticker, report, cancellationToken);
        }

        return report;
    }

    public async Task<List<ResearchExperimentDto>> GetExperimentsAsync(
        string ticker,
        CancellationToken cancellationToken = default)
    {
        var upperTicker = ticker.ToUpper();

        var experiments = await _context.ResearchExperiments
            .AsNoTracking()
            .Include(e => e.Ticker)
            .Where(e => e.Ticker.Symbol == upperTicker)
            .OrderByDescending(e => e.CreatedAt)
            .Select(e => new ResearchExperimentDto
            {
                Id = e.Id,
                Ticker = e.Ticker.Symbol,
                FeatureName = e.FeatureName,
                StartDate = e.StartDate,
                EndDate = e.EndDate,
                BarsUsed = e.BarsUsed,
                MeanIC = (double)e.MeanIC,
                ICTStat = (double)e.ICTStat,
                ICPValue = (double)e.ICPValue,
                AdfPValue = (double)e.AdfPValue,
                KpssPValue = (double)e.KpssPValue,
                IsStationary = e.IsStationary,
                PassedValidation = e.PassedValidation,
                MonotonicityRatio = (double)e.MonotonicityRatio,
                IsMonotonic = e.IsMonotonic,
                CreatedAt = e.CreatedAt,
            })
            .ToListAsync(cancellationToken);

        return experiments;
    }

    public async Task<ResearchExperimentDto?> GetExperimentAsync(
        int id,
        CancellationToken cancellationToken = default)
    {
        var experiment = await _context.ResearchExperiments
            .AsNoTracking()
            .Include(e => e.Ticker)
            .Where(e => e.Id == id)
            .Select(e => new ResearchExperimentDto
            {
                Id = e.Id,
                Ticker = e.Ticker.Symbol,
                FeatureName = e.FeatureName,
                StartDate = e.StartDate,
                EndDate = e.EndDate,
                BarsUsed = e.BarsUsed,
                MeanIC = (double)e.MeanIC,
                ICTStat = (double)e.ICTStat,
                ICPValue = (double)e.ICPValue,
                AdfPValue = (double)e.AdfPValue,
                KpssPValue = (double)e.KpssPValue,
                IsStationary = e.IsStationary,
                PassedValidation = e.PassedValidation,
                MonotonicityRatio = (double)e.MonotonicityRatio,
                IsMonotonic = e.IsMonotonic,
                CreatedAt = e.CreatedAt,
            })
            .FirstOrDefaultAsync(cancellationToken);

        return experiment;
    }

    public async Task<SignalEngineReportDto> RunSignalEngineAsync(
        string ticker,
        string featureName,
        string fromDate,
        string toDate,
        bool flipSign = true,
        bool regimeGateEnabled = true,
        string timespan = "minute",
        int multiplier = 1,
        bool forceRefresh = false,
        CancellationToken cancellationToken = default)
    {
        _logger.LogInformation(
            "[Signal] Running {Feature} on {Ticker} from {From} to {To}",
            featureName, ticker, fromDate, toDate);

        var signalFetchResult = await _marketDataService.GetOrFetchAggregatesAsync(
            ticker.ToUpper(), multiplier, timespan, fromDate, toDate,
            forceRefresh: forceRefresh, cancellationToken: cancellationToken);
        var aggregates = signalFetchResult.Aggregates;

        if (aggregates.Count == 0)
        {
            return new SignalEngineReportDto
            {
                Success = false,
                Ticker = ticker,
                FeatureName = featureName,
                Error = $"No aggregates found for {ticker} in date range {fromDate} to {toDate}"
            };
        }

        _logger.LogInformation(
            "[Signal] Found {Count} aggregates for {Ticker}, sending to Python",
            aggregates.Count, ticker);

        var bars = aggregates.Select(a => new OhlcvBarDto(
            new DateTimeOffset(a.Timestamp, TimeSpan.Zero).ToUnixTimeMilliseconds(),
            a.Open, a.High, a.Low, a.Close, a.Volume
        )).ToList();

        var request = new RunSignalEngineRequest
        {
            Ticker = ticker.ToUpper(),
            FeatureName = featureName,
            Bars = bars,
            StartDate = fromDate,
            EndDate = toDate,
            FlipSign = flipSign,
            RegimeGateEnabled = regimeGateEnabled,
        };

        var response = await _httpClient.PostAsJsonAsync(
            "/api/research/run-signal", request, _jsonOptions, cancellationToken);

        response.EnsureSuccessStatusCode();

        var report = await response.Content.ReadFromJsonAsync<SignalEngineReportDto>(
            _jsonOptions, cancellationToken);

        if (report is null)
            throw new HttpRequestException("Failed to parse signal engine report from Python service");

        _logger.LogInformation(
            "[Signal] Completed {Feature} on {Ticker}: grade={Grade}, status={Status}",
            featureName, ticker,
            report.Graduation?.OverallGrade ?? "N/A",
            report.Graduation?.StatusLabel ?? "N/A");

        if (report.Success)
        {
            await PersistSignalExperimentAsync(ticker, report, cancellationToken);
        }

        return report;
    }

    public async Task<List<SignalExperimentDto>> GetSignalExperimentsAsync(
        string ticker,
        CancellationToken cancellationToken = default)
    {
        var upperTicker = ticker.ToUpper();

        return await _context.SignalExperiments
            .AsNoTracking()
            .Include(e => e.Ticker)
            .Where(e => e.Ticker.Symbol == upperTicker)
            .OrderByDescending(e => e.CreatedAt)
            .Select(e => new SignalExperimentDto
            {
                Id = e.Id,
                Ticker = e.Ticker.Symbol,
                FeatureName = e.FeatureName,
                StartDate = e.StartDate,
                EndDate = e.EndDate,
                BarsUsed = e.BarsUsed,
                OverallGrade = e.OverallGrade,
                StatusLabel = e.StatusLabel,
                OverallPassed = e.OverallPassed,
                MeanOosSharpe = (double)e.MeanOosSharpe,
                BestThreshold = (double)e.BestThreshold,
                BestCostBps = (double)e.BestCostBps,
                FlipSign = e.FlipSign,
                RegimeGateEnabled = e.RegimeGateEnabled,
                CreatedAt = e.CreatedAt,
            })
            .ToListAsync(cancellationToken);
    }

    public async Task<SignalEngineReportDto?> GetSignalExperimentReportAsync(
        int id,
        CancellationToken cancellationToken = default)
    {
        var jsonReport = await _context.SignalExperiments
            .AsNoTracking()
            .Where(e => e.Id == id)
            .Select(e => e.JsonReport)
            .FirstOrDefaultAsync(cancellationToken);

        if (jsonReport is null)
            return null;

        return JsonSerializer.Deserialize<SignalEngineReportDto>(jsonReport, _jsonOptions);
    }

    private async Task PersistSignalExperimentAsync(
        string ticker,
        SignalEngineReportDto report,
        CancellationToken cancellationToken)
    {
        try
        {
            var market = ticker.StartsWith("O:", StringComparison.OrdinalIgnoreCase) ? "options" : "stocks";
            var tickerEntity = await _marketDataService.GetOrCreateTickerAsync(
                ticker.ToUpper(), market, cancellationToken);

            var experiment = new SignalExperiment
            {
                TickerId = tickerEntity.Id,
                FeatureName = report.FeatureName,
                StartDate = report.StartDate,
                EndDate = report.EndDate,
                BarsUsed = report.BarsUsed,
                OverallGrade = report.Graduation?.OverallGrade ?? "N/A",
                StatusLabel = report.Graduation?.StatusLabel ?? "N/A",
                OverallPassed = report.Graduation?.OverallPassed ?? false,
                MeanOosSharpe = (decimal)(report.WalkForward?.MeanOosSharpe ?? 0),
                BestThreshold = (decimal)report.BestThreshold,
                BestCostBps = (decimal)report.BestCostBps,
                FlipSign = report.FlipSign,
                RegimeGateEnabled = report.Methodology?.RegimeGateEnabled ?? true,
                JsonReport = JsonSerializer.Serialize(report, _jsonOptions),
            };

            _context.SignalExperiments.Add(experiment);
            await _context.SaveChangesAsync(cancellationToken);

            _logger.LogInformation(
                "[Signal] Persisted experiment {Id} for {Ticker}/{Feature}",
                experiment.Id, ticker, report.FeatureName);
        }
        catch (Exception ex)
        {
            _logger.LogWarning(ex,
                "[Signal] Failed to persist experiment for {Ticker}/{Feature} — result still returned",
                ticker, report.FeatureName);
        }
    }

    public async Task<ResearchReportDto> RunOptionsFeatureResearchAsync(
        string ticker,
        string featureName,
        string fromDate,
        string toDate,
        string targetType = "directional",
        CancellationToken cancellationToken = default)
    {
        _logger.LogInformation(
            "[Options Research] Running {Feature} on {Ticker} from {From} to {To} (target={Target})",
            featureName, ticker, fromDate, toDate, targetType);

        // Step 1: Get IV data from cache
        var upperTicker = ticker.ToUpper();
        var tickerEntity = await _context.Tickers
            .AsNoTracking()
            .FirstOrDefaultAsync(t => t.Symbol == upperTicker, cancellationToken);

        List<IvDataPointDto> ivData = [];

        if (tickerEntity != null)
        {
            var startDate = DateTime.Parse(fromDate);
            var endDate = DateTime.Parse(toDate);

            ivData = await _context.OptionsIvSnapshots
                .AsNoTracking()
                .Where(s => s.TickerId == tickerEntity.Id
                    && s.TradingDate >= startDate
                    && s.TradingDate <= endDate)
                .OrderBy(s => s.TradingDate)
                .Select(s => new IvDataPointDto
                {
                    Date = s.TradingDate.ToString("yyyy-MM-dd"),
                    AtmIv = s.Iv30dAtm != null ? (double)s.Iv30dAtm : null,
                    IvOtmPut = s.Iv30dPut != null ? (double)s.Iv30dPut : null,
                    IvOtmCall = s.Iv30dCall != null ? (double)s.Iv30dCall : null,
                    StockClose = s.StockClose != null ? (double)s.StockClose : null,
                })
                .ToListAsync(cancellationToken);
        }

        if (ivData.Count == 0)
        {
            _logger.LogInformation("[Options Research] No cached IV data, triggering Python derivation");

            // Trigger IV derivation via Python service
            var buildRequest = new BuildIvHistoryRequest
            {
                UnderlyingTicker = upperTicker,
                StartDate = fromDate,
                EndDate = toDate,
            };

            var buildResponse = await _httpClient.PostAsJsonAsync(
                "/api/research/build-iv-history", buildRequest, _jsonOptions, cancellationToken);
            buildResponse.EnsureSuccessStatusCode();

            var buildResult = await buildResponse.Content.ReadFromJsonAsync<BuildIvHistoryResponseDto>(
                _jsonOptions, cancellationToken);

            if (buildResult is null || !buildResult.Success || buildResult.IvData.Count == 0)
            {
                return new ResearchReportDto
                {
                    Success = false,
                    Ticker = ticker,
                    FeatureName = featureName,
                    Error = buildResult?.Error ?? "Failed to derive IV history",
                };
            }

            // Convert and cache
            ivData = buildResult.IvData.Select(d => new IvDataPointDto
            {
                Date = d.GetValueOrDefault("date")?.ToString() ?? "",
                AtmIv = d.GetValueOrDefault("iv_30d_atm") is { } atm ? Convert.ToDouble(atm) : null,
                IvOtmPut = d.GetValueOrDefault("iv_30d_put") is { } put ? Convert.ToDouble(put) : null,
                IvOtmCall = d.GetValueOrDefault("iv_30d_call") is { } call ? Convert.ToDouble(call) : null,
                StockClose = d.GetValueOrDefault("stock_close") is { } sc ? Convert.ToDouble(sc) : null,
            }).Where(p => !string.IsNullOrEmpty(p.Date)).ToList();

            // Persist IV data to cache
            await PersistIvDataAsync(upperTicker, buildResult.IvData, cancellationToken);
        }

        // Step 2: Get daily stock bars
        var stockFetchResult = await _marketDataService.GetOrFetchAggregatesAsync(
            upperTicker, 1, "day", fromDate, toDate,
            forceRefresh: false, cancellationToken: cancellationToken);

        var dailyBars = stockFetchResult.Aggregates
            .Select(a => new OhlcvBarDto(
                new DateTimeOffset(a.Timestamp, TimeSpan.Zero).ToUnixTimeMilliseconds(),
                a.Open, a.High, a.Low, a.Close, a.Volume))
            .ToList();

        // Step 3: Send to Python
        var request = new RunOptionsFeatureResearchRequest
        {
            Ticker = upperTicker,
            FeatureName = featureName,
            IvData = ivData,
            StockDailyBars = dailyBars,
            StartDate = fromDate,
            EndDate = toDate,
            TargetType = targetType,
        };

        var response = await _httpClient.PostAsJsonAsync(
            "/api/research/run-options-feature", request, _jsonOptions, cancellationToken);
        response.EnsureSuccessStatusCode();

        var report = await response.Content.ReadFromJsonAsync<ResearchReportDto>(
            _jsonOptions, cancellationToken);

        if (report is null)
            throw new HttpRequestException("Failed to parse options research report from Python service");

        if (report.Success)
        {
            await PersistExperimentAsync(ticker, report, cancellationToken);
        }

        return report;
    }

    public async Task<BatchResearchResultDto> RunBatchOptionsResearchAsync(
        string featureName,
        List<string> tickers,
        string fromDate,
        string toDate,
        string targetType = "directional",
        CancellationToken cancellationToken = default)
    {
        _logger.LogInformation(
            "[Batch Options] Running {Feature} across {Count} tickers",
            featureName, tickers.Count);

        var request = new RunBatchOptionsResearchRequest
        {
            FeatureName = featureName,
            Tickers = tickers.Select(t => t.ToUpper()).ToList(),
            StartDate = fromDate,
            EndDate = toDate,
            TargetType = targetType,
        };

        var response = await _httpClient.PostAsJsonAsync(
            "/api/research/run-batch-options", request, _jsonOptions, cancellationToken);
        response.EnsureSuccessStatusCode();

        var report = await response.Content.ReadFromJsonAsync<BatchResearchResultDto>(
            _jsonOptions, cancellationToken);

        if (report is null)
            throw new HttpRequestException("Failed to parse batch research report");

        return report;
    }

    private async Task PersistIvDataAsync(
        string ticker,
        List<Dictionary<string, object?>> ivRecords,
        CancellationToken cancellationToken)
    {
        try
        {
            var tickerEntity = await _marketDataService.GetOrCreateTickerAsync(
                ticker, "stocks", cancellationToken);

            var snapshots = new List<OptionsIvSnapshot>();

            foreach (var record in ivRecords)
            {
                var dateStr = record.GetValueOrDefault("date")?.ToString();
                if (string.IsNullOrEmpty(dateStr)) continue;

                if (!DateTime.TryParse(dateStr, out var tradingDate)) continue;

                snapshots.Add(new OptionsIvSnapshot
                {
                    TickerId = tickerEntity.Id,
                    TradingDate = tradingDate,
                    Iv30dAtm = record.GetValueOrDefault("iv_30d_atm") is { } atm ? (decimal)Convert.ToDouble(atm) : null,
                    Iv30dPut = record.GetValueOrDefault("iv_30d_put") is { } put ? (decimal)Convert.ToDouble(put) : null,
                    Iv30dCall = record.GetValueOrDefault("iv_30d_call") is { } call ? (decimal)Convert.ToDouble(call) : null,
                    StockClose = record.GetValueOrDefault("stock_close") is { } sc ? (decimal)Convert.ToDouble(sc) : null,
                    DteLow = record.GetValueOrDefault("dte_low") is { } dl ? Convert.ToInt32(dl) : null,
                    DteHigh = record.GetValueOrDefault("dte_high") is { } dh ? Convert.ToInt32(dh) : null,
                    PriceSource = record.GetValueOrDefault("price_source")?.ToString() ?? "",
                    Source = "derived",
                });
            }

            _context.OptionsIvSnapshots.AddRange(snapshots);
            await _context.SaveChangesAsync(cancellationToken);

            _logger.LogInformation(
                "[Options Research] Cached {Count} IV snapshots for {Ticker}",
                snapshots.Count, ticker);
        }
        catch (Exception ex)
        {
            _logger.LogWarning(ex,
                "[Options Research] Failed to cache IV data for {Ticker}", ticker);
        }
    }

    private async Task PersistExperimentAsync(
        string ticker,
        ResearchReportDto report,
        CancellationToken cancellationToken)
    {
        try
        {
            var market = ticker.StartsWith("O:", StringComparison.OrdinalIgnoreCase) ? "options" : "stocks";
            var tickerEntity = await _marketDataService.GetOrCreateTickerAsync(
                ticker.ToUpper(), market, cancellationToken);

            var experiment = new ResearchExperiment
            {
                TickerId = tickerEntity.Id,
                FeatureName = report.FeatureName,
                StartDate = report.StartDate,
                EndDate = report.EndDate,
                BarsUsed = report.BarsUsed,
                MeanIC = (decimal)report.MeanIc,
                ICTStat = (decimal)report.IcTStat,
                ICPValue = (decimal)report.IcPValue,
                AdfPValue = (decimal)report.AdfPvalue,
                KpssPValue = (decimal)report.KpssPvalue,
                IsStationary = report.IsStationary,
                PassedValidation = report.PassedValidation,
                MonotonicityRatio = (decimal)report.MonotonicityRatio,
                IsMonotonic = report.IsMonotonic,
                JsonReport = JsonSerializer.Serialize(report, _jsonOptions),
            };

            _context.ResearchExperiments.Add(experiment);
            await _context.SaveChangesAsync(cancellationToken);

            _logger.LogInformation(
                "[Research] Persisted experiment {Id} for {Ticker}/{Feature}",
                experiment.Id, ticker, report.FeatureName);
        }
        catch (Exception ex)
        {
            _logger.LogWarning(ex,
                "[Research] Failed to persist experiment for {Ticker}/{Feature} — result still returned",
                ticker, report.FeatureName);
        }
    }
}

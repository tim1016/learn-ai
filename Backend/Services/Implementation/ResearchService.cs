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
        var aggregates = await _marketDataService.GetOrFetchAggregatesAsync(
            ticker.ToUpper(), multiplier, timespan, fromDate, toDate,
            forceRefresh: false, cancellationToken: cancellationToken);

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
        CancellationToken cancellationToken = default)
    {
        _logger.LogInformation(
            "[Signal] Running {Feature} on {Ticker} from {From} to {To}",
            featureName, ticker, fromDate, toDate);

        var aggregates = await _marketDataService.GetOrFetchAggregatesAsync(
            ticker.ToUpper(), multiplier, timespan, fromDate, toDate,
            forceRefresh: false, cancellationToken: cancellationToken);

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

        return report;
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

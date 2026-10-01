using System.Text.Json;
using Backend.Data;
using Backend.Models.DTOs;
using Backend.Services.Interfaces;
using Backend.Temporal;
using Microsoft.EntityFrameworkCore;

namespace Backend.Services.Implementation;

/// <summary>
/// Reads the Research Lab history tables. Runs start through <c>/api/jobs</c>,
/// not through this service.
/// </summary>
public class ResearchService : IResearchService
{
    private readonly AppDbContext _context;

    private static readonly JsonSerializerOptions _jsonOptions = new()
    {
        PropertyNamingPolicy = JsonNamingPolicy.SnakeCaseLower
    };

    public ResearchService(AppDbContext context)
    {
        _context = context;
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
                CreatedAt = UnixMs.FromUtc(e.CreatedAt),
            })
            .ToListAsync(cancellationToken);

        return experiments;
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
                CreatedAt = UnixMs.FromUtc(e.CreatedAt),
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
}

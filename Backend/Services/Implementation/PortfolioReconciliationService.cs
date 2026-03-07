using Backend.Data;
using Backend.Models.Portfolio;
using Backend.Services.Interfaces;
using Microsoft.EntityFrameworkCore;
using Microsoft.Extensions.Logging;

namespace Backend.Services.Implementation;

public class PortfolioReconciliationService : IPortfolioReconciliationService
{
    private readonly AppDbContext _context;
    private readonly IPositionEngine _positionEngine;
    private readonly ILogger<PortfolioReconciliationService> _logger;

    public PortfolioReconciliationService(AppDbContext context, IPositionEngine positionEngine,
        ILogger<PortfolioReconciliationService> logger)
    {
        _context = context;
        _positionEngine = positionEngine;
        _logger = logger;
    }

    public async Task<ReconciliationReport> ReconcileAsync(Guid accountId, CancellationToken ct = default)
    {
        _logger.LogInformation("[Reconciliation] Starting for account {AccountId}", accountId);

        // Snapshot current cached positions
        var cachedPositions = await _context.Positions
            .Include(p => p.Ticker)
            .Where(p => p.AccountId == accountId)
            .AsNoTracking()
            .ToListAsync(ct);

        // Rebuild from trades
        var rebuiltPositions = await _positionEngine.RebuildPositionsAsync(accountId, ct);

        // Reload rebuilt positions with ticker data
        rebuiltPositions = await _context.Positions
            .Include(p => p.Ticker)
            .Where(p => p.AccountId == accountId)
            .ToListAsync(ct);

        var drifts = new List<PositionDrift>();

        // Build lookup by (tickerId, optionContractId, status) for comparison
        var cachedLookup = cachedPositions
            .GroupBy(p => (p.TickerId, p.OptionContractId, p.Status))
            .ToDictionary(g => g.Key, g => g.First());

        var rebuiltLookup = rebuiltPositions
            .GroupBy(p => (p.TickerId, p.OptionContractId, p.Status))
            .ToDictionary(g => g.Key, g => g.First());

        // Check for mismatches
        var allKeys = cachedLookup.Keys.Union(rebuiltLookup.Keys).Distinct();

        foreach (var key in allKeys)
        {
            var hasCached = cachedLookup.TryGetValue(key, out var cached);
            var hasRebuilt = rebuiltLookup.TryGetValue(key, out var rebuilt);

            if (hasCached && hasRebuilt)
            {
                if (cached!.NetQuantity != rebuilt!.NetQuantity
                    || Math.Abs(cached.RealizedPnL - rebuilt.RealizedPnL) > 0.01m)
                {
                    drifts.Add(new PositionDrift
                    {
                        TickerId = key.TickerId,
                        Symbol = cached.Ticker?.Symbol ?? rebuilt.Ticker?.Symbol ?? "?",
                        CachedQuantity = cached.NetQuantity,
                        RebuiltQuantity = rebuilt.NetQuantity,
                        CachedRealizedPnL = cached.RealizedPnL,
                        RebuiltRealizedPnL = rebuilt.RealizedPnL,
                        DriftType = "Mismatch",
                    });
                }
            }
            else if (hasCached && !hasRebuilt)
            {
                drifts.Add(new PositionDrift
                {
                    TickerId = key.TickerId,
                    Symbol = cached!.Ticker?.Symbol ?? "?",
                    CachedQuantity = cached.NetQuantity,
                    RebuiltQuantity = 0,
                    CachedRealizedPnL = cached.RealizedPnL,
                    RebuiltRealizedPnL = 0,
                    DriftType = "ExtraInCache",
                });
            }
            else if (!hasCached && hasRebuilt)
            {
                drifts.Add(new PositionDrift
                {
                    TickerId = key.TickerId,
                    Symbol = rebuilt!.Ticker?.Symbol ?? "?",
                    CachedQuantity = 0,
                    RebuiltQuantity = rebuilt.NetQuantity,
                    CachedRealizedPnL = 0,
                    RebuiltRealizedPnL = rebuilt.RealizedPnL,
                    DriftType = "MissingFromCache",
                });
            }
        }

        _logger.LogInformation("[Reconciliation] Found {Count} drifts", drifts.Count);

        return new ReconciliationReport
        {
            AccountId = accountId,
            HasDrift = drifts.Count > 0,
            Drifts = drifts,
            CachedPositionCount = cachedPositions.Count,
            RebuiltPositionCount = rebuiltPositions.Count,
        };
    }

    public async Task AutoFixAsync(Guid accountId, CancellationToken ct = default)
    {
        _logger.LogInformation("[Reconciliation] AutoFix: rebuilding positions for account {AccountId}", accountId);
        await _positionEngine.RebuildPositionsAsync(accountId, ct);
    }
}

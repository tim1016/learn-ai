using Backend.Data;
using Backend.Models.DTOs.PolygonResponses;
using Backend.Models.Portfolio;
using Backend.Services.Interfaces;
using Microsoft.EntityFrameworkCore;
using Microsoft.Extensions.Logging;

namespace Backend.Services.Implementation;

public class PortfolioRiskService : IPortfolioRiskService
{
    private readonly AppDbContext _context;
    private readonly IPortfolioValuationService _valuationService;
    private readonly IPolygonService _polygonService;
    private readonly ILogger<PortfolioRiskService> _logger;

    public PortfolioRiskService(AppDbContext context, IPortfolioValuationService valuationService,
        IPolygonService polygonService,
        ILogger<PortfolioRiskService> logger)
    {
        _context = context;
        _valuationService = valuationService;
        _polygonService = polygonService;
        _logger = logger;
    }

    // ----------------------------------------------------------------------
    // STALE-GREEK NOTICE (Phase 2 of numerical-authority-migration-plan.md):
    //
    // The two methods below — ComputeDollarDeltaAsync and ComputePortfolioVegaAsync
    // — still source per-position Greeks from `OptionLeg.EntryDelta` /
    // `OptionLeg.EntryVega`. Those are entry-time stored values; they do
    // not reflect current spot/IV. The aggregate sum is rule-5 compliant
    // (.NET aggregating Python-supplied Greeks is fine), but the *Greeks
    // themselves* should come from `IPolygonService.PortfolioLiveGreeksAsync`
    // — see RunScenarioAsync below for the pattern.
    //
    // Migration follow-up: replace the `latestLeg?.EntryXxx` lookups in
    // these methods with a call to PortfolioLiveGreeksAsync that returns
    // recomputed Greeks at current spot/IV. Tracked in
    // docs/math-sources-of-truth.md § Portfolio.
    // ----------------------------------------------------------------------

    public async Task<List<DollarDeltaResult>> ComputeDollarDeltaAsync(Guid accountId,
        Dictionary<string, decimal> prices, CancellationToken ct = default)
    {
        var positions = await _context.Positions
            .Include(p => p.Ticker)
            .Include(p => p.OptionContract)
            .Where(p => p.AccountId == accountId && p.Status == PositionStatus.Open)
            .ToListAsync(ct);

        var results = new List<DollarDeltaResult>();

        foreach (var position in positions)
        {
            var symbol = position.Ticker.Symbol;
            if (!prices.TryGetValue(symbol, out var price)) continue;

            var multiplier = position.OptionContractId.HasValue
                ? (position.OptionContract?.Multiplier ?? 100)
                : 1;

            decimal delta;
            if (position.AssetType == AssetType.Stock)
            {
                delta = 1m; // Stocks have delta = 1
            }
            else
            {
                // Get latest entry delta from option legs
                var latestLeg = await _context.OptionLegs
                    .Where(l => l.OptionContractId == position.OptionContractId
                                && l.Trade.AccountId == accountId)
                    .OrderByDescending(l => l.Trade.ExecutionTimestamp)
                    .FirstOrDefaultAsync(ct);

                delta = latestLeg?.EntryDelta ?? 0;
            }

            var dollarDelta = delta * price * position.NetQuantity * multiplier;

            results.Add(new DollarDeltaResult
            {
                PositionId = position.Id,
                Symbol = symbol,
                Delta = delta,
                Price = price,
                Quantity = position.NetQuantity,
                Multiplier = multiplier,
                DollarDelta = dollarDelta,
            });
        }

        return results;
    }

    public async Task<decimal> ComputePortfolioVegaAsync(Guid accountId, CancellationToken ct = default)
    {
        var optionPositions = await _context.Positions
            .Include(p => p.OptionContract)
            .Where(p => p.AccountId == accountId
                        && p.Status == PositionStatus.Open
                        && p.AssetType == AssetType.Option)
            .ToListAsync(ct);

        decimal totalVega = 0;

        foreach (var position in optionPositions)
        {
            var multiplier = position.OptionContract?.Multiplier ?? 100;

            var latestLeg = await _context.OptionLegs
                .Where(l => l.OptionContractId == position.OptionContractId
                            && l.Trade.AccountId == accountId)
                .OrderByDescending(l => l.Trade.ExecutionTimestamp)
                .FirstOrDefaultAsync(ct);

            if (latestLeg?.EntryVega != null)
                totalVega += latestLeg.EntryVega.Value * position.NetQuantity * multiplier;
        }

        return totalVega;
    }

    public async Task<List<RiskViolation>> EvaluateRiskRulesAsync(Guid accountId,
        Dictionary<string, decimal> prices, CancellationToken ct = default)
    {
        var rules = await _context.RiskRules
            .Where(r => r.AccountId == accountId && r.Enabled)
            .ToListAsync(ct);

        if (rules.Count == 0) return [];

        var valuation = await _valuationService.ComputeValuationWithPricesAsync(accountId, prices, ct);
        var violations = new List<RiskViolation>();

        foreach (var rule in rules)
        {
            decimal actualValue = 0;
            bool violated = false;

            switch (rule.RuleType)
            {
                case RiskRuleType.MaxDrawdown:
                    var snapshots = await _context.PortfolioSnapshots
                        .Where(s => s.AccountId == accountId)
                        .OrderBy(s => s.Timestamp)
                        .Select(s => s.Equity)
                        .ToListAsync(ct);

                    if (snapshots.Count > 0)
                    {
                        var peak = snapshots.Max();
                        var drawdown = peak > 0 ? (peak - valuation.Equity) / peak : 0;
                        actualValue = drawdown;
                        violated = drawdown > rule.Threshold;
                    }
                    break;

                case RiskRuleType.MaxPositionSize:
                    if (valuation.Equity > 0)
                    {
                        foreach (var pos in valuation.Positions)
                        {
                            var pctOfPortfolio = Math.Abs(pos.MarketValue) / valuation.Equity;
                            if (pctOfPortfolio > actualValue)
                                actualValue = pctOfPortfolio;
                        }
                        violated = actualValue > rule.Threshold;
                    }
                    break;

                case RiskRuleType.MaxVegaExposure:
                    actualValue = Math.Abs(await ComputePortfolioVegaAsync(accountId, ct));
                    violated = actualValue > rule.Threshold;
                    break;

                case RiskRuleType.MaxDelta:
                    var deltas = await ComputeDollarDeltaAsync(accountId, prices, ct);
                    actualValue = Math.Abs(deltas.Sum(d => d.DollarDelta));
                    violated = actualValue > rule.Threshold;
                    break;
            }

            if (violated)
            {
                rule.LastTriggered = DateTime.UtcNow;
                violations.Add(new RiskViolation
                {
                    RuleId = rule.Id,
                    RuleType = rule.RuleType,
                    Action = rule.Action,
                    Severity = rule.Severity,
                    Threshold = rule.Threshold,
                    ActualValue = actualValue,
                    Message = $"{rule.RuleType}: {actualValue:P2} exceeds threshold {rule.Threshold:P2}",
                });
            }
        }

        if (violations.Count > 0)
            await _context.SaveChangesAsync(ct);

        return violations;
    }

    public async Task<ScenarioResult> RunScenarioAsync(Guid accountId,
        Dictionary<string, decimal> prices, ScenarioInput scenario, CancellationToken ct = default)
    {
        // Phase 2.2 of `docs/architecture/numerical-authority-migration-plan.md`:
        // Scenario math is now Python-canonical. This method projects DB state
        // into the Python `/api/portfolio/scenario` request shape, calls the
        // endpoint, and aggregates the response. Greeks are recomputed at
        // scenario time using current spot/IV — no stale entry-Greek shock
        // propagation. See `app/services/portfolio_scenario.py`.

        var valuation = await _valuationService.ComputeValuationWithPricesAsync(accountId, prices, ct);

        // Group positions by underlying so each scenario call has a single
        // underlying (the Python endpoint validates this).
        var positions = await _context.Positions
            .Include(p => p.Ticker)
            .Include(p => p.OptionContract).ThenInclude(c => c!.UnderlyingTicker)
            .Where(p => p.AccountId == accountId && p.Status == PositionStatus.Open)
            .ToListAsync(ct);

        var byUnderlying = positions
            .GroupBy(p => p.OptionContract?.UnderlyingTicker.Symbol ?? p.Ticker.Symbol)
            .ToList();

        var nowMs = DateTimeOffset.UtcNow.ToUnixTimeMilliseconds();
        var positionScenarios = new List<PositionScenario>();
        decimal scenarioMarketValue = 0;

        foreach (var group in byUnderlying)
        {
            var underlying = group.Key;
            if (!prices.TryGetValue(underlying, out var spot)) continue;

            var pythonPositions = await ProjectPositionsForPythonAsync(
                accountId, group.ToList(), ct);
            if (pythonPositions.Count == 0) continue;

            var grid = new PortfolioScenarioGridDto
            {
                SpotShocks = [scenario.PriceChangePercent ?? 0m],
                TimeShiftsDays = [(decimal)(scenario.TimeDaysForward ?? 0)],
                IvShifts = [scenario.IvChangePercent ?? 0m],
            };

            var pythonResult = await _polygonService.PortfolioScenarioAsync(
                nowMs, spot, pythonPositions, grid,
                cancellationToken: ct);

            var point = pythonResult.Points.FirstOrDefault();
            if (point == null) continue;

            // Re-aggregate per-position into ScenarioResult shape. The Python
            // payload reports the portfolio P&L; we synthesize per-position
            // ScenarioValue as (entry_price + per_share_pnl) × quantity × multiplier
            // from each leg row.
            for (var i = 0; i < pythonPositions.Count; i++)
            {
                var pyPos = pythonPositions[i];
                var leg = i < point.Legs.Count ? point.Legs[i] : null;
                if (leg == null) continue;

                var multiplier = pyPos.Multiplier ?? 1m;
                var scenarioValue = leg.TheoreticalPrice * pyPos.Quantity * multiplier;
                var currentPositionValue = valuation.Positions
                    .FirstOrDefault(v => v.Symbol == pyPos.Symbol)?.MarketValue ?? 0m;

                positionScenarios.Add(new PositionScenario
                {
                    Symbol = pyPos.Symbol,
                    CurrentValue = currentPositionValue,
                    ScenarioValue = scenarioValue,
                    PnLImpact = scenarioValue - currentPositionValue,
                });

                scenarioMarketValue += scenarioValue;
            }
        }

        var scenarioEquity = valuation.Cash + scenarioMarketValue;
        var pnlImpact = scenarioEquity - valuation.Equity;

        return new ScenarioResult
        {
            CurrentEquity = valuation.Equity,
            ScenarioEquity = scenarioEquity,
            PnLImpact = pnlImpact,
            PnLImpactPercent = valuation.Equity != 0 ? pnlImpact / valuation.Equity : 0,
            Positions = positionScenarios,
        };
    }

    /// <summary>
    /// Project a set of positions for one underlying into the Python
    /// `/api/portfolio/scenario` request shape. Stocks become StockPosition
    /// rows; options become OptionPosition rows with strike/expiration/IV
    /// pulled from <see cref="OptionContract"/> + the latest stored
    /// <see cref="OptionLeg.EntryIV"/>.
    ///
    /// Note: <see cref="OptionLeg.EntryIV"/> is itself stored at entry time —
    /// it's the best per-contract IV we currently have on file. Phase 2 fixes
    /// stale *Greek* propagation; replacing stored IV with a current Polygon
    /// snapshot is a follow-up improvement.
    /// </summary>
    private async Task<List<PortfolioScenarioPositionDto>> ProjectPositionsForPythonAsync(
        Guid accountId,
        List<Position> positions,
        CancellationToken ct)
    {
        var result = new List<PortfolioScenarioPositionDto>();
        foreach (var pos in positions)
        {
            if (pos.AssetType == AssetType.Stock)
            {
                result.Add(new PortfolioScenarioPositionDto
                {
                    Instrument = "stock",
                    Symbol = pos.Ticker.Symbol,
                    Quantity = pos.NetQuantity,
                    EntryPrice = pos.AvgCostBasis,
                    LegId = pos.Id.ToString(),
                });
                continue;
            }

            if (pos.OptionContract == null) continue;

            var contract = pos.OptionContract;
            var latestLeg = await _context.OptionLegs
                .Where(l => l.OptionContractId == pos.OptionContractId
                            && l.Trade.AccountId == accountId)
                .OrderByDescending(l => l.Trade.ExecutionTimestamp)
                .FirstOrDefaultAsync(ct);

            // EntryIV is decimal? — fall back to a sane default if missing.
            // The fallback is logged so it's traceable, not silent.
            decimal currentIv;
            if (latestLeg?.EntryIV is { } iv && iv > 0m)
            {
                currentIv = iv;
            }
            else
            {
                currentIv = 0.25m;
                _logger.LogWarning(
                    "[Portfolio] Position {PositionId} for {Symbol} has no stored EntryIV; using fallback {Iv}",
                    pos.Id, contract.UnderlyingTicker.Symbol, currentIv);
            }

            var expirationMs = new DateTimeOffset(
                contract.Expiration.ToDateTime(new TimeOnly(20, 0)),
                TimeSpan.Zero).ToUnixTimeMilliseconds();

            result.Add(new PortfolioScenarioPositionDto
            {
                Instrument = "option",
                Symbol = contract.UnderlyingTicker.Symbol,
                OptionType = contract.OptionType == OptionType.Call ? "call" : "put",
                Strike = contract.Strike,
                ExpirationMs = expirationMs,
                Quantity = pos.NetQuantity,
                Multiplier = contract.Multiplier,
                EntryPrice = pos.AvgCostBasis,
                CurrentIv = currentIv,
                LegId = pos.Id.ToString(),
            });
        }
        return result;
    }
}

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
    // Phase 2.3 — closure of the STALE-GREEK NOTICE.
    //
    // ComputeDollarDeltaAsync and ComputePortfolioVegaAsync now source
    // per-position Greeks from `IPolygonService.PortfolioLiveGreeksAsync`
    // (Python recomputes against current spot/IV) instead of stored
    // `OptionLeg.EntryDelta` / `EntryVega`. Stocks short-circuit to
    // delta=1 and don't hit Python. The methods only aggregate; per
    // rule-5 that's compliant.
    //
    // ComputePortfolioVegaAsync's GraphQL resolver `getPortfolioVega`
    // takes only (accountId) — no prices — so this method fetches the
    // underlying spots itself via `_polygonService.FetchStockSnapshotsAsync`.
    // ComputeDollarDeltaAsync receives `prices` from the caller (existing
    // contract) and uses them directly.
    // ----------------------------------------------------------------------

    public async Task<List<DollarDeltaResult>> ComputeDollarDeltaAsync(Guid accountId,
        Dictionary<string, decimal> prices, CancellationToken ct = default)
    {
        var positions = await _context.Positions
            .Include(p => p.Ticker)
            .Include(p => p.OptionContract).ThenInclude(c => c!.UnderlyingTicker)
            .Where(p => p.AccountId == accountId && p.Status == PositionStatus.Open)
            .ToListAsync(ct);

        var results = new List<DollarDeltaResult>();

        // Stocks: delta = 1, no Python call needed.
        foreach (var pos in positions.Where(p => p.AssetType == AssetType.Stock))
        {
            var symbol = pos.Ticker.Symbol;
            if (!prices.TryGetValue(symbol, out var price)) continue;
            var dollarDelta = price * pos.NetQuantity;
            results.Add(new DollarDeltaResult
            {
                PositionId = pos.Id,
                Symbol = symbol,
                Delta = 1m,
                Price = price,
                Quantity = pos.NetQuantity,
                Multiplier = 1,
                DollarDelta = dollarDelta,
            });
        }

        // Options: group by underlying, fetch live Greeks per underlying.
        var optionGroups = positions
            .Where(p => p.AssetType == AssetType.Option && p.OptionContract != null)
            .GroupBy(p => p.OptionContract!.UnderlyingTicker.Symbol);

        var nowMs = DateTimeOffset.UtcNow.ToUnixTimeMilliseconds();

        foreach (var group in optionGroups)
        {
            var underlying = group.Key;
            if (!prices.TryGetValue(underlying, out var spot)) continue;

            var pythonPositions = await ProjectPositionsForPythonAsync(
                accountId, group.ToList(), ct);
            if (pythonPositions.Count == 0) continue;

            var liveGreeks = await _polygonService.PortfolioLiveGreeksAsync(
                nowMs, spot, pythonPositions, cancellationToken: ct);

            var point = liveGreeks.Points.FirstOrDefault();
            if (point == null) continue;

            for (var i = 0; i < pythonPositions.Count; i++)
            {
                var pyPos = pythonPositions[i];
                var leg = i < point.Legs.Count ? point.Legs[i] : null;
                if (leg == null) continue;

                var pos = group.First(p => p.Id.ToString() == pyPos.LegId);
                var multiplier = (int)(pyPos.Multiplier ?? 100m);
                var dollarDelta = leg.Delta * spot * pos.NetQuantity * multiplier;

                results.Add(new DollarDeltaResult
                {
                    PositionId = pos.Id,
                    Symbol = pos.Ticker.Symbol,
                    Delta = leg.Delta,
                    Price = spot,
                    Quantity = pos.NetQuantity,
                    Multiplier = multiplier,
                    DollarDelta = dollarDelta,
                });
            }
        }

        return results;
    }

    public async Task<decimal> ComputePortfolioVegaAsync(Guid accountId, CancellationToken ct = default)
    {
        var optionPositions = await _context.Positions
            .Include(p => p.OptionContract).ThenInclude(c => c!.UnderlyingTicker)
            .Where(p => p.AccountId == accountId
                        && p.Status == PositionStatus.Open
                        && p.AssetType == AssetType.Option
                        && p.OptionContract != null)
            .ToListAsync(ct);

        if (optionPositions.Count == 0) return 0m;

        var optionGroups = optionPositions
            .GroupBy(p => p.OptionContract!.UnderlyingTicker.Symbol)
            .ToList();

        // Fetch underlying spots in one batched snapshot call. Resolver
        // doesn't take prices, so we source them ourselves.
        var underlyings = optionGroups.Select(g => g.Key).ToList();
        var snapshots = await _polygonService.FetchStockSnapshotsAsync(underlyings, ct);
        var spots = snapshots.Snapshots
            .Where(s => s.Ticker != null)
            .Select(s => new
            {
                s.Ticker,
                Spot = s.Min?.Close ?? s.Day?.Close,
            })
            .Where(x => x.Spot.HasValue)
            .ToDictionary(x => x.Ticker!, x => x.Spot!.Value);

        var nowMs = DateTimeOffset.UtcNow.ToUnixTimeMilliseconds();
        decimal totalVega = 0;

        foreach (var group in optionGroups)
        {
            var underlying = group.Key;
            if (!spots.TryGetValue(underlying, out var spot))
            {
                _logger.LogWarning(
                    "[Portfolio] No spot snapshot for {Underlying}; skipping vega contribution",
                    underlying);
                continue;
            }

            var pythonPositions = await ProjectPositionsForPythonAsync(
                accountId, group.ToList(), ct);
            if (pythonPositions.Count == 0) continue;

            var liveGreeks = await _polygonService.PortfolioLiveGreeksAsync(
                nowMs, spot, pythonPositions, cancellationToken: ct);

            var point = liveGreeks.Points.FirstOrDefault();
            if (point == null) continue;

            for (var i = 0; i < pythonPositions.Count; i++)
            {
                var pyPos = pythonPositions[i];
                var leg = i < point.Legs.Count ? point.Legs[i] : null;
                if (leg == null) continue;
                var multiplier = pyPos.Multiplier ?? 100m;
                totalVega += leg.Vega * pyPos.Quantity * multiplier;
            }
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
                // Join by position identity (LegId), NOT by symbol. For options,
                // pyPos.Symbol is the underlying ticker — two SPY option legs
                // would otherwise both attach to the same SPY valuation row and
                // lose per-position identity.
                var currentPositionValue = valuation.Positions
                    .FirstOrDefault(v => v.PositionId.ToString() == pyPos.LegId)?.MarketValue ?? 0m;

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

            // Per rule-5 (.NET is transport, never picks model inputs):
            // when EntryIV is missing, skip this position and log a warning.
            // .NET does NOT fabricate an IV — that would make it a math
            // authority. The trade-off: positions without a stored IV are
            // omitted from the scenario/delta/vega totals. Caller sees
            // accurate data-coverage rather than fake math.
            if (latestLeg?.EntryIV is not { } iv || iv <= 0m)
            {
                _logger.LogWarning(
                    "[Portfolio] Position {PositionId} for {Symbol} has no stored EntryIV; skipped (no fake IV substituted)",
                    pos.Id, contract.UnderlyingTicker.Symbol);
                continue;
            }
            var currentIv = iv;

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

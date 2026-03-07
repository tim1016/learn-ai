using Backend.Data;
using Backend.Models.Portfolio;
using Backend.Services.Interfaces;
using Microsoft.EntityFrameworkCore;
using Microsoft.Extensions.Logging;

namespace Backend.Services.Implementation;

public class PortfolioRiskService : IPortfolioRiskService
{
    private readonly AppDbContext _context;
    private readonly IPortfolioValuationService _valuationService;
    private readonly ILogger<PortfolioRiskService> _logger;

    public PortfolioRiskService(AppDbContext context, IPortfolioValuationService valuationService,
        ILogger<PortfolioRiskService> logger)
    {
        _context = context;
        _valuationService = valuationService;
        _logger = logger;
    }

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
        var valuation = await _valuationService.ComputeValuationWithPricesAsync(accountId, prices, ct);

        var positionScenarios = new List<PositionScenario>();
        decimal scenarioMarketValue = 0;

        foreach (var pos in valuation.Positions)
        {
            var scenarioPrice = pos.CurrentPrice;

            // Apply price shock
            if (scenario.PriceChangePercent.HasValue)
                scenarioPrice *= (1 + scenario.PriceChangePercent.Value);

            var scenarioValue = scenarioPrice * pos.Quantity * pos.Multiplier;

            // Apply IV change impact via vega (for options only)
            if (scenario.IvChangePercent.HasValue && pos.Multiplier > 1)
            {
                var latestLeg = await _context.OptionLegs
                    .Where(l => l.Trade.AccountId == accountId)
                    .OrderByDescending(l => l.Trade.ExecutionTimestamp)
                    .FirstOrDefaultAsync(l => l.OptionContract.UnderlyingTicker.Symbol == pos.Symbol, ct);

                if (latestLeg?.EntryVega != null)
                    scenarioValue += latestLeg.EntryVega.Value * scenario.IvChangePercent.Value
                                     * pos.Quantity * pos.Multiplier;
            }

            // Apply theta decay
            if (scenario.TimeDaysForward.HasValue && pos.Multiplier > 1)
            {
                var latestLeg = await _context.OptionLegs
                    .Where(l => l.Trade.AccountId == accountId)
                    .OrderByDescending(l => l.Trade.ExecutionTimestamp)
                    .FirstOrDefaultAsync(l => l.OptionContract.UnderlyingTicker.Symbol == pos.Symbol, ct);

                if (latestLeg?.EntryTheta != null)
                    scenarioValue += latestLeg.EntryTheta.Value * scenario.TimeDaysForward.Value
                                     * pos.Quantity * pos.Multiplier;
            }

            positionScenarios.Add(new PositionScenario
            {
                Symbol = pos.Symbol,
                CurrentValue = pos.MarketValue,
                ScenarioValue = scenarioValue,
                PnLImpact = scenarioValue - pos.MarketValue,
            });

            scenarioMarketValue += scenarioValue;
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
}

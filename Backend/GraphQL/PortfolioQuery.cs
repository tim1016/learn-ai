using Backend.Data;
using Backend.Models.Portfolio;
using Backend.Services.Interfaces;
using HotChocolate;
using Microsoft.EntityFrameworkCore;

namespace Backend.GraphQL;

public class PriceInput
{
    public string Symbol { get; set; } = null!;
    public decimal Price { get; set; }
}

[ExtendObjectType<Query>]
public class PortfolioQuery
{
    [UseProjection]
    [UseFiltering]
    [UseSorting]
    [GraphQLName("getAccounts")]
    public IQueryable<Account> GetAccounts(AppDbContext context)
        => context.Accounts;

    [UseFirstOrDefault]
    [UseProjection]
    [GraphQLName("getAccount")]
    public IQueryable<Account?> GetAccount(AppDbContext context, Guid id)
        => context.Accounts.Where(a => a.Id == id);

    [UseProjection]
    [UseFiltering]
    [UseSorting]
    [GraphQLName("getPositions")]
    public IQueryable<Position> GetPositions(AppDbContext context, Guid accountId)
        => context.Positions
            .Include(p => p.Lots)
            .Include(p => p.Ticker)
            .Where(p => p.AccountId == accountId);

    [UseFirstOrDefault]
    [UseProjection]
    [GraphQLName("getPosition")]
    public IQueryable<Position?> GetPosition(AppDbContext context, Guid id)
        => context.Positions
            .Include(p => p.Lots)
            .Include(p => p.Ticker)
            .Where(p => p.Id == id);

    [UseProjection]
    [UseFiltering]
    [UseSorting]
    [GraphQLName("getPortfolioTrades")]
    public IQueryable<PortfolioTrade> GetPortfolioTrades(AppDbContext context, Guid accountId)
        => context.PortfolioTrades
            .Include(t => t.Ticker)
            .Include(t => t.OptionLeg)
            .Where(t => t.AccountId == accountId);

    [UseProjection]
    [UseFiltering]
    [UseSorting]
    [GraphQLName("getPositionLots")]
    public IQueryable<PositionLot> GetPositionLots(AppDbContext context, Guid positionId)
        => context.PositionLots.Where(l => l.PositionId == positionId);

    [GraphQLName("getPortfolioState")]
    public async Task<PortfolioState> GetPortfolioState(
        [Service] IPortfolioService portfolioService,
        Guid accountId)
    {
        return await portfolioService.GetPortfolioStateAsync(accountId);
    }

    #region Phase 2 — Valuation & Snapshots

    [GraphQLName("getPortfolioValuation")]
    public async Task<PortfolioValuation> GetPortfolioValuation(
        [Service] IPortfolioValuationService valuationService,
        Guid accountId)
    {
        return await valuationService.ComputeValuationAsync(accountId);
    }

    [UseProjection]
    [UseFiltering]
    [UseSorting]
    [GraphQLName("getPortfolioSnapshots")]
    public IQueryable<PortfolioSnapshot> GetPortfolioSnapshots(AppDbContext context, Guid accountId)
        => context.PortfolioSnapshots
            .Where(s => s.AccountId == accountId)
            .OrderBy(s => s.Timestamp);

    [GraphQLName("getEquityCurve")]
    public async Task<List<PortfolioSnapshot>> GetEquityCurve(
        [Service] ISnapshotService snapshotService,
        Guid accountId,
        DateTime? from = null,
        DateTime? to = null)
    {
        return await snapshotService.GetEquityCurveAsync(accountId, from, to);
    }

    [GraphQLName("getDrawdownSeries")]
    public async Task<List<DrawdownPoint>> GetDrawdownSeries(
        [Service] ISnapshotService snapshotService,
        Guid accountId)
    {
        return await snapshotService.GetDrawdownSeriesAsync(accountId);
    }

    [GraphQLName("getPortfolioMetrics")]
    public async Task<PortfolioMetrics> GetPortfolioMetrics(
        [Service] ISnapshotService snapshotService,
        Guid accountId)
    {
        var snapshots = await snapshotService.GetEquityCurveAsync(accountId);
        return snapshotService.ComputeMetrics(snapshots);
    }

    #endregion

    #region Phase 3 — Risk & Reconciliation

    [UseProjection]
    [UseFiltering]
    [UseSorting]
    [GraphQLName("getRiskRules")]
    public IQueryable<RiskRule> GetRiskRules(AppDbContext context, Guid accountId)
        => context.RiskRules.Where(r => r.AccountId == accountId);

    [GraphQLName("getDollarDelta")]
    public async Task<List<DollarDeltaResult>> GetDollarDelta(
        [Service] IPortfolioRiskService riskService,
        Guid accountId,
        List<PriceInput> prices)
    {
        var priceDict = prices.ToDictionary(p => p.Symbol, p => p.Price);
        return await riskService.ComputeDollarDeltaAsync(accountId, priceDict);
    }

    [GraphQLName("getPortfolioVega")]
    public async Task<decimal> GetPortfolioVega(
        [Service] IPortfolioRiskService riskService,
        Guid accountId)
    {
        return await riskService.ComputePortfolioVegaAsync(accountId);
    }

    [GraphQLName("evaluateRiskRules")]
    public async Task<List<RiskViolation>> EvaluateRiskRules(
        [Service] IPortfolioRiskService riskService,
        Guid accountId,
        List<PriceInput> prices)
    {
        var priceDict = prices.ToDictionary(p => p.Symbol, p => p.Price);
        return await riskService.EvaluateRiskRulesAsync(accountId, priceDict);
    }

    [GraphQLName("reconcilePortfolio")]
    public async Task<ReconciliationReport> ReconcilePortfolio(
        [Service] IPortfolioReconciliationService reconciliationService,
        Guid accountId)
    {
        return await reconciliationService.ReconcileAsync(accountId);
    }

    #endregion

    #region Phase 4 — Strategy Attribution

    [GraphQLName("getStrategyPnL")]
    [GraphQLDescription("Get PnL breakdown for a specific strategy execution")]
    public async Task<StrategyPnLResult> GetStrategyPnL(
        [Service] IStrategyAttributionService attributionService,
        int strategyExecutionId)
    {
        return await attributionService.GetStrategyPnLAsync(strategyExecutionId);
    }

    [GraphQLName("getAlphaAttribution")]
    public async Task<List<AlphaAttribution>> GetAlphaAttribution(
        [Service] IStrategyAttributionService attributionService,
        Guid accountId)
    {
        return await attributionService.GetAlphaAttributionAsync(accountId);
    }

    [UseProjection]
    [UseFiltering]
    [UseSorting]
    [GraphQLName("getStrategyAllocations")]
    public IQueryable<StrategyAllocation> GetStrategyAllocations(AppDbContext context, Guid accountId)
        => context.StrategyAllocations
            .Include(a => a.StrategyExecution)
            .Where(a => a.AccountId == accountId);

    #endregion
}

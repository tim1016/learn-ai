using Backend.Models.Portfolio;
using Backend.Services.Interfaces;
using HotChocolate;
using Microsoft.Extensions.Logging;

// Validation result types are in Backend.Models.Portfolio.ValidationResult

namespace Backend.GraphQL;

[ExtendObjectType<Mutation>]
public class PortfolioMutation
{
    [GraphQLName("createAccount")]
    public async Task<AccountResult> CreateAccount(
        [Service] IPortfolioService portfolioService,
        string name,
        string type = "Paper",
        decimal initialCash = 100_000m)
    {
        try
        {
            var accountType = Enum.Parse<AccountType>(type, ignoreCase: true);
            var account = await portfolioService.CreateAccountAsync(name, accountType, initialCash);
            return new AccountResult { Success = true, Account = account };
        }
        catch (Exception ex)
        {
            return new AccountResult { Success = false, Error = ex.Message };
        }
    }

    [GraphQLName("submitOrder")]
    public async Task<OrderResult> SubmitOrder(
        [Service] IPortfolioService portfolioService,
        Guid accountId,
        int tickerId,
        string side,
        string orderType = "Market",
        decimal quantity = 1,
        decimal? limitPrice = null,
        string assetType = "Stock",
        Guid? optionContractId = null)
    {
        try
        {
            var order = await portfolioService.SubmitOrderAsync(
                accountId, tickerId,
                Enum.Parse<OrderSide>(side, ignoreCase: true),
                Enum.Parse<OrderType>(orderType, ignoreCase: true),
                quantity, limitPrice,
                Enum.Parse<AssetType>(assetType, ignoreCase: true),
                optionContractId);
            return new OrderResult { Success = true, Order = order };
        }
        catch (Exception ex)
        {
            return new OrderResult { Success = false, Error = ex.Message };
        }
    }

    [GraphQLName("cancelOrder")]
    public async Task<OrderResult> CancelOrder(
        [Service] IPortfolioService portfolioService,
        Guid orderId)
    {
        try
        {
            var order = await portfolioService.CancelOrderAsync(orderId);
            return new OrderResult { Success = true, Order = order };
        }
        catch (Exception ex)
        {
            return new OrderResult { Success = false, Error = ex.Message };
        }
    }

    [GraphQLName("fillOrder")]
    public async Task<TradeResult> FillOrder(
        [Service] IPortfolioService portfolioService,
        Guid orderId,
        decimal fillPrice,
        decimal fillQuantity,
        decimal fees = 0,
        int multiplier = 1)
    {
        try
        {
            var trade = await portfolioService.FillOrderAsync(
                orderId, fillPrice, fillQuantity, fees, multiplier);
            return new TradeResult { Success = true, Trade = trade };
        }
        catch (Exception ex)
        {
            return new TradeResult { Success = false, Error = ex.Message };
        }
    }

    [GraphQLName("recordTrade")]
    public async Task<TradeResult> RecordTrade(
        [Service] IPortfolioService portfolioService,
        Guid accountId,
        string symbol,
        string side,
        decimal quantity,
        decimal price,
        decimal fees = 0,
        string assetType = "Stock",
        int multiplier = 1)
    {
        try
        {
            var input = new RecordTradeInput
            {
                AccountId = accountId,
                Symbol = symbol.Trim().ToUpperInvariant(),
                Side = Enum.Parse<OrderSide>(side, ignoreCase: true),
                Quantity = quantity,
                Price = price,
                Fees = fees,
                AssetType = Enum.Parse<AssetType>(assetType, ignoreCase: true),
                Multiplier = multiplier,
            };
            var trade = await portfolioService.RecordTradeAsync(input);
            return new TradeResult { Success = true, Trade = trade };
        }
        catch (Exception ex)
        {
            return new TradeResult { Success = false, Error = ex.Message };
        }
    }

    [GraphQLName("rebuildPositions")]
    public async Task<RebuildResult> RebuildPositions(
        [Service] IPositionEngine positionEngine,
        [Service] ILogger<PortfolioMutation> logger,
        Guid accountId)
    {
        try
        {
            logger.LogInformation("[Portfolio] Rebuilding positions for account {AccountId}", accountId);
            var positions = await positionEngine.RebuildPositionsAsync(accountId);
            return new RebuildResult
            {
                Success = true,
                PositionCount = positions.Count,
                Message = $"Rebuilt {positions.Count} positions from trade log",
            };
        }
        catch (Exception ex)
        {
            logger.LogError(ex, "[Portfolio] Error rebuilding positions for {AccountId}", accountId);
            return new RebuildResult { Success = false, Error = ex.Message };
        }
    }

    #region Phase 2 — Snapshots

    [GraphQLName("takePortfolioSnapshot")]
    public async Task<SnapshotResult> TakePortfolioSnapshot(
        [Service] ISnapshotService snapshotService,
        [Service] ILogger<PortfolioMutation> logger,
        Guid accountId)
    {
        try
        {
            var snapshot = await snapshotService.TakeSnapshotAsync(accountId);
            return new SnapshotResult
            {
                Success = true,
                Snapshot = snapshot,
                Message = $"Snapshot captured: Equity={snapshot.Equity:C}",
            };
        }
        catch (Exception ex)
        {
            logger.LogError(ex, "[Snapshot] Error taking snapshot for {AccountId}", accountId);
            return new SnapshotResult { Success = false, Error = ex.Message };
        }
    }

    #endregion

    #region Phase 3 — Risk Rules & Scenarios

    [GraphQLName("createRiskRule")]
    public async Task<RiskRuleResult> CreateRiskRule(
        [Service] IPortfolioRiskService _,
        Backend.Data.AppDbContext context,
        Guid accountId,
        string ruleType,
        decimal threshold,
        string action = "Warn",
        string severity = "Medium")
    {
        try
        {
            var rule = new RiskRule
            {
                Id = Guid.NewGuid(),
                AccountId = accountId,
                RuleType = Enum.Parse<RiskRuleType>(ruleType, ignoreCase: true),
                Threshold = threshold,
                Action = Enum.Parse<RiskAction>(action, ignoreCase: true),
                Severity = Enum.Parse<RiskSeverity>(severity, ignoreCase: true),
                Enabled = true,
            };
            context.RiskRules.Add(rule);
            await context.SaveChangesAsync();
            return new RiskRuleResult { Success = true, Rule = rule };
        }
        catch (Exception ex)
        {
            return new RiskRuleResult { Success = false, Error = ex.Message };
        }
    }

    [GraphQLName("updateRiskRule")]
    public async Task<RiskRuleResult> UpdateRiskRule(
        Backend.Data.AppDbContext context,
        Guid ruleId,
        decimal? threshold = null,
        bool? enabled = null,
        string? action = null,
        string? severity = null)
    {
        try
        {
            var rule = await context.RiskRules.FindAsync(ruleId)
                ?? throw new InvalidOperationException($"Risk rule {ruleId} not found");

            if (threshold.HasValue) rule.Threshold = threshold.Value;
            if (enabled.HasValue) rule.Enabled = enabled.Value;
            if (action != null) rule.Action = Enum.Parse<RiskAction>(action, ignoreCase: true);
            if (severity != null) rule.Severity = Enum.Parse<RiskSeverity>(severity, ignoreCase: true);

            await context.SaveChangesAsync();
            return new RiskRuleResult { Success = true, Rule = rule };
        }
        catch (Exception ex)
        {
            return new RiskRuleResult { Success = false, Error = ex.Message };
        }
    }

    [GraphQLName("runScenario")]
    public async Task<ScenarioResult> RunScenario(
        [Service] IPortfolioRiskService riskService,
        Guid accountId,
        List<PriceInput> prices,
        decimal? priceChangePercent = null,
        decimal? ivChangePercent = null,
        int? timeDaysForward = null)
    {
        var priceDict = prices.ToDictionary(p => p.Symbol, p => p.Price);
        var scenario = new ScenarioInput
        {
            PriceChangePercent = priceChangePercent,
            IvChangePercent = ivChangePercent,
            TimeDaysForward = timeDaysForward,
        };
        return await riskService.RunScenarioAsync(accountId, priceDict, scenario);
    }

    [GraphQLName("autoFixPortfolio")]
    public async Task<RebuildResult> AutoFixPortfolio(
        [Service] IPortfolioReconciliationService reconciliationService,
        [Service] ILogger<PortfolioMutation> logger,
        Guid accountId)
    {
        try
        {
            await reconciliationService.AutoFixAsync(accountId);
            return new RebuildResult
            {
                Success = true,
                Message = "Portfolio positions rebuilt from trade log",
            };
        }
        catch (Exception ex)
        {
            logger.LogError(ex, "[Reconciliation] AutoFix error for {AccountId}", accountId);
            return new RebuildResult { Success = false, Error = ex.Message };
        }
    }

    #endregion

    #region Phase 4 — Strategy Attribution

    [GraphQLName("linkTradeToStrategy")]
    public async Task<LinkResult> LinkTradeToStrategy(
        [Service] IStrategyAttributionService attributionService,
        Guid tradeId,
        int strategyExecutionId)
    {
        try
        {
            var link = await attributionService.LinkTradeToStrategyAsync(tradeId, strategyExecutionId);
            return new LinkResult { Success = true, Link = link };
        }
        catch (Exception ex)
        {
            return new LinkResult { Success = false, Error = ex.Message };
        }
    }

    [GraphQLName("importBacktestTrades")]
    public async Task<ImportResult> ImportBacktestTrades(
        [Service] IStrategyAttributionService attributionService,
        int strategyExecutionId,
        Guid accountId)
    {
        try
        {
            var trades = await attributionService.ImportBacktestTradesAsync(strategyExecutionId, accountId);
            return new ImportResult
            {
                Success = true,
                TradeCount = trades.Count,
                Message = $"Imported {trades.Count} trades from strategy execution {strategyExecutionId}",
            };
        }
        catch (Exception ex)
        {
            return new ImportResult { Success = false, Error = ex.Message };
        }
    }

    #endregion

    #region Phase 5 — Validation

    [GraphQLName("runPortfolioValidation")]
    public async Task<ValidationSuiteResult> RunPortfolioValidation(
        [Service] IPortfolioValidationService validationService,
        [Service] ILogger<PortfolioMutation> logger)
    {
        logger.LogInformation("[Validation] Starting portfolio validation suite");
        return await validationService.RunValidationSuiteAsync();
    }

    #endregion
}

public class RiskRuleResult
{
    public bool Success { get; set; }
    public RiskRule? Rule { get; set; }
    public string? Error { get; set; }
}

public class LinkResult
{
    public bool Success { get; set; }
    public StrategyTradeLink? Link { get; set; }
    public string? Error { get; set; }
}

public class ImportResult
{
    public bool Success { get; set; }
    public int TradeCount { get; set; }
    public string? Message { get; set; }
    public string? Error { get; set; }
}

public class SnapshotResult
{
    public bool Success { get; set; }
    public PortfolioSnapshot? Snapshot { get; set; }
    public string? Message { get; set; }
    public string? Error { get; set; }
}

public class AccountResult
{
    public bool Success { get; set; }
    public Account? Account { get; set; }
    public string? Error { get; set; }
}

public class OrderResult
{
    public bool Success { get; set; }
    public Order? Order { get; set; }
    public string? Error { get; set; }
}

public class TradeResult
{
    public bool Success { get; set; }
    public PortfolioTrade? Trade { get; set; }
    public string? Error { get; set; }
}

public class RebuildResult
{
    public bool Success { get; set; }
    public int PositionCount { get; set; }
    public string? Message { get; set; }
    public string? Error { get; set; }
}

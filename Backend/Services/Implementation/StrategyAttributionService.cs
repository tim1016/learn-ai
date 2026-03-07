using Backend.Data;
using Backend.Models.MarketData;
using Backend.Models.Portfolio;
using Backend.Services.Interfaces;
using Microsoft.EntityFrameworkCore;
using Microsoft.Extensions.Logging;

namespace Backend.Services.Implementation;

public class StrategyAttributionService : IStrategyAttributionService
{
    private readonly AppDbContext _context;
    private readonly IPositionEngine _positionEngine;
    private readonly ILogger<StrategyAttributionService> _logger;

    public StrategyAttributionService(AppDbContext context, IPositionEngine positionEngine,
        ILogger<StrategyAttributionService> logger)
    {
        _context = context;
        _positionEngine = positionEngine;
        _logger = logger;
    }

    public async Task<StrategyTradeLink> LinkTradeToStrategyAsync(Guid tradeId, int strategyExecutionId,
        CancellationToken ct = default)
    {
        var trade = await _context.PortfolioTrades.FindAsync([tradeId], ct)
            ?? throw new InvalidOperationException($"Trade {tradeId} not found");

        var strategy = await _context.StrategyExecutions.FindAsync([strategyExecutionId], ct)
            ?? throw new InvalidOperationException($"Strategy execution {strategyExecutionId} not found");

        var link = new StrategyTradeLink
        {
            Id = Guid.NewGuid(),
            TradeId = tradeId,
            StrategyExecutionId = strategyExecutionId,
        };

        _context.StrategyTradeLinks.Add(link);
        await _context.SaveChangesAsync(ct);

        _logger.LogInformation("[Attribution] Linked trade {TradeId} to strategy {StrategyId}",
            tradeId, strategyExecutionId);
        return link;
    }

    public async Task<List<PortfolioTrade>> ImportBacktestTradesAsync(int strategyExecutionId, Guid accountId,
        CancellationToken ct = default)
    {
        var account = await _context.Accounts.FindAsync([accountId], ct)
            ?? throw new InvalidOperationException($"Account {accountId} not found");

        var execution = await _context.StrategyExecutions
            .Include(e => e.Trades)
            .Include(e => e.Ticker)
            .FirstOrDefaultAsync(e => e.Id == strategyExecutionId, ct)
            ?? throw new InvalidOperationException($"Strategy execution {strategyExecutionId} not found");

        var portfolioTrades = new List<PortfolioTrade>();

        foreach (var bt in execution.Trades.OrderBy(t => t.EntryTimestamp))
        {
            // Create buy trade at entry
            var buyOrder = new Order
            {
                Id = Guid.NewGuid(),
                AccountId = accountId,
                TickerId = execution.TickerId,
                Side = OrderSide.Buy,
                OrderType = OrderType.Market,
                Quantity = bt.Quantity,
                Status = OrderStatus.Filled,
                SubmittedAt = bt.EntryTimestamp,
                FilledAt = bt.EntryTimestamp,
            };
            _context.Orders.Add(buyOrder);

            var buyTrade = new PortfolioTrade
            {
                Id = Guid.NewGuid(),
                AccountId = accountId,
                OrderId = buyOrder.Id,
                TickerId = execution.TickerId,
                Side = OrderSide.Buy,
                Quantity = bt.Quantity,
                Price = bt.EntryPrice,
                Fees = 0,
                Multiplier = 1,
                ExecutionTimestamp = bt.EntryTimestamp,
            };
            _context.PortfolioTrades.Add(buyTrade);
            portfolioTrades.Add(buyTrade);

            // Link buy trade
            _context.StrategyTradeLinks.Add(new StrategyTradeLink
            {
                Id = Guid.NewGuid(),
                TradeId = buyTrade.Id,
                StrategyExecutionId = strategyExecutionId,
            });

            // Create sell trade at exit
            var sellOrder = new Order
            {
                Id = Guid.NewGuid(),
                AccountId = accountId,
                TickerId = execution.TickerId,
                Side = OrderSide.Sell,
                OrderType = OrderType.Market,
                Quantity = bt.Quantity,
                Status = OrderStatus.Filled,
                SubmittedAt = bt.ExitTimestamp,
                FilledAt = bt.ExitTimestamp,
            };
            _context.Orders.Add(sellOrder);

            var sellTrade = new PortfolioTrade
            {
                Id = Guid.NewGuid(),
                AccountId = accountId,
                OrderId = sellOrder.Id,
                TickerId = execution.TickerId,
                Side = OrderSide.Sell,
                Quantity = bt.Quantity,
                Price = bt.ExitPrice,
                Fees = 0,
                Multiplier = 1,
                ExecutionTimestamp = bt.ExitTimestamp,
            };
            _context.PortfolioTrades.Add(sellTrade);
            portfolioTrades.Add(sellTrade);

            // Link sell trade
            _context.StrategyTradeLinks.Add(new StrategyTradeLink
            {
                Id = Guid.NewGuid(),
                TradeId = sellTrade.Id,
                StrategyExecutionId = strategyExecutionId,
            });
        }

        // Create strategy allocation record
        _context.StrategyAllocations.Add(new StrategyAllocation
        {
            Id = Guid.NewGuid(),
            AccountId = accountId,
            StrategyExecutionId = strategyExecutionId,
            CapitalAllocated = account.InitialCash,
            StartDate = execution.Trades.Min(t => t.EntryTimestamp),
            EndDate = execution.Trades.Max(t => t.ExitTimestamp),
        });

        await _context.SaveChangesAsync(ct);

        // Apply trades to position engine
        foreach (var trade in portfolioTrades.OrderBy(t => t.ExecutionTimestamp))
        {
            await _positionEngine.ApplyTradeAsync(trade, ct);
        }

        _logger.LogInformation(
            "[Attribution] Imported {Count} backtest trades from strategy {StrategyId} into account {AccountId}",
            portfolioTrades.Count, strategyExecutionId, accountId);

        return portfolioTrades;
    }

    public async Task<StrategyPnLResult> GetStrategyPnLAsync(int strategyExecutionId,
        CancellationToken ct = default)
    {
        var execution = await _context.StrategyExecutions
            .FirstOrDefaultAsync(e => e.Id == strategyExecutionId, ct)
            ?? throw new InvalidOperationException($"Strategy execution {strategyExecutionId} not found");

        var linkedTrades = await _context.StrategyTradeLinks
            .Include(l => l.Trade)
            .Where(l => l.StrategyExecutionId == strategyExecutionId)
            .Select(l => l.Trade)
            .ToListAsync(ct);

        // Compute PnL from linked position lots
        var tradeIds = linkedTrades.Select(t => t.Id).ToHashSet();
        var lots = await _context.PositionLots
            .Where(l => tradeIds.Contains(l.TradeId))
            .ToListAsync(ct);

        var totalPnL = lots.Sum(l => l.RealizedPnL);
        var closedLots = lots.Where(l => l.ClosedAt.HasValue).ToList();
        var winningLots = closedLots.Count(l => l.RealizedPnL > 0);
        var winRate = closedLots.Count > 0 ? (decimal)winningLots / closedLots.Count : 0;

        return new StrategyPnLResult
        {
            StrategyExecutionId = strategyExecutionId,
            StrategyName = execution.StrategyName,
            TotalPnL = totalPnL,
            TradeCount = linkedTrades.Count,
            WinRate = winRate,
        };
    }

    public async Task<List<AlphaAttribution>> GetAlphaAttributionAsync(Guid accountId,
        CancellationToken ct = default)
    {
        var allocations = await _context.StrategyAllocations
            .Include(a => a.StrategyExecution)
            .Where(a => a.AccountId == accountId)
            .ToListAsync(ct);

        var attributions = new List<AlphaAttribution>();
        decimal totalPnL = 0;

        foreach (var alloc in allocations)
        {
            var pnlResult = await GetStrategyPnLAsync(alloc.StrategyExecutionId, ct);
            totalPnL += pnlResult.TotalPnL;

            attributions.Add(new AlphaAttribution
            {
                StrategyExecutionId = alloc.StrategyExecutionId,
                StrategyName = alloc.StrategyExecution.StrategyName,
                PnL = pnlResult.TotalPnL,
                TradeCount = pnlResult.TradeCount,
            });
        }

        // Compute contribution percentages
        if (totalPnL != 0)
        {
            foreach (var attr in attributions)
                attr.ContributionPercent = attr.PnL / totalPnL;
        }

        return attributions;
    }
}

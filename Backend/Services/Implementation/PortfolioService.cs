using Backend.Data;
using Backend.Models.MarketData;
using Backend.Models.Portfolio;
using Backend.Services.Interfaces;
using Microsoft.EntityFrameworkCore;
using Microsoft.Extensions.Logging;

namespace Backend.Services.Implementation;

public class PortfolioService : IPortfolioService
{
    private readonly AppDbContext _context;
    private readonly IPositionEngine _positionEngine;
    private readonly ILogger<PortfolioService> _logger;

    public PortfolioService(AppDbContext context, IPositionEngine positionEngine, ILogger<PortfolioService> logger)
    {
        _context = context;
        _positionEngine = positionEngine;
        _logger = logger;
    }

    public async Task<Account> CreateAccountAsync(string name, AccountType type, decimal initialCash,
        CancellationToken ct = default)
    {
        var account = new Account
        {
            Id = Guid.NewGuid(),
            Name = name,
            Type = type,
            InitialCash = initialCash,
            Cash = initialCash,
            CreatedAt = DateTime.UtcNow,
        };

        _context.Accounts.Add(account);
        await _context.SaveChangesAsync(ct);

        _logger.LogInformation("[Portfolio] Created account {Name} ({Type}) with {Cash:C}", name, type, initialCash);
        return account;
    }

    public async Task<Order> SubmitOrderAsync(Guid accountId, int tickerId, OrderSide side, OrderType orderType,
        decimal quantity, decimal? limitPrice, AssetType assetType = AssetType.Stock,
        Guid? optionContractId = null, CancellationToken ct = default)
    {
        var account = await _context.Accounts.FindAsync([accountId], ct)
            ?? throw new InvalidOperationException($"Account {accountId} not found");

        var order = new Order
        {
            Id = Guid.NewGuid(),
            AccountId = accountId,
            TickerId = tickerId,
            Side = side,
            OrderType = orderType,
            Quantity = quantity,
            LimitPrice = limitPrice,
            AssetType = assetType,
            OptionContractId = optionContractId,
            Status = OrderStatus.Pending,
            SubmittedAt = DateTime.UtcNow,
        };

        _context.Orders.Add(order);
        await _context.SaveChangesAsync(ct);

        _logger.LogInformation("[Portfolio] Order submitted: {Side} {Qty} ticker {TickerId}", side, quantity, tickerId);
        return order;
    }

    public async Task<Order> CancelOrderAsync(Guid orderId, CancellationToken ct = default)
    {
        var order = await _context.Orders.FindAsync([orderId], ct)
            ?? throw new InvalidOperationException($"Order {orderId} not found");

        if (order.Status != OrderStatus.Pending && order.Status != OrderStatus.PartiallyFilled)
            throw new InvalidOperationException($"Cannot cancel order with status {order.Status}");

        order.Status = OrderStatus.Cancelled;
        await _context.SaveChangesAsync(ct);

        _logger.LogInformation("[Portfolio] Order {OrderId} cancelled", orderId);
        return order;
    }

    public async Task<PortfolioTrade> FillOrderAsync(Guid orderId, decimal fillPrice, decimal fillQuantity,
        decimal fees = 0, int multiplier = 1, OptionLegInput? optionLeg = null, CancellationToken ct = default)
    {
        var order = await _context.Orders.FindAsync([orderId], ct)
            ?? throw new InvalidOperationException($"Order {orderId} not found");

        if (order.Status == OrderStatus.Filled || order.Status == OrderStatus.Cancelled)
            throw new InvalidOperationException($"Cannot fill order with status {order.Status}");

        var trade = new PortfolioTrade
        {
            Id = Guid.NewGuid(),
            AccountId = order.AccountId,
            OrderId = order.Id,
            TickerId = order.TickerId,
            Side = order.Side,
            Quantity = fillQuantity,
            Price = fillPrice,
            Fees = fees,
            AssetType = order.AssetType,
            OptionContractId = order.OptionContractId,
            Multiplier = multiplier,
            ExecutionTimestamp = DateTime.UtcNow,
        };

        _context.PortfolioTrades.Add(trade);

        // Create option leg if provided
        if (optionLeg != null)
        {
            var leg = new OptionLeg
            {
                Id = Guid.NewGuid(),
                TradeId = trade.Id,
                OptionContractId = optionLeg.OptionContractId,
                Quantity = optionLeg.Quantity,
                EntryIV = optionLeg.EntryIV,
                EntryDelta = optionLeg.EntryDelta,
                EntryGamma = optionLeg.EntryGamma,
                EntryTheta = optionLeg.EntryTheta,
                EntryVega = optionLeg.EntryVega,
            };
            _context.OptionLegs.Add(leg);
        }

        // Update cash
        var account = await _context.Accounts.FindAsync([order.AccountId], ct)
            ?? throw new InvalidOperationException($"Account {order.AccountId} not found");

        var totalCost = fillPrice * fillQuantity * multiplier + fees;
        if (order.Side == OrderSide.Buy)
            account.Cash -= totalCost;
        else
            account.Cash += fillPrice * fillQuantity * multiplier - fees;

        // Update order status
        order.FilledAt = DateTime.UtcNow;
        order.Status = fillQuantity >= order.Quantity ? OrderStatus.Filled : OrderStatus.PartiallyFilled;

        await _context.SaveChangesAsync(ct);

        // Apply to position engine
        await _positionEngine.ApplyTradeAsync(trade, ct);

        _logger.LogInformation("[Portfolio] Order {OrderId} filled: {Qty} @ {Price}", orderId, fillQuantity, fillPrice);
        return trade;
    }

    public async Task<PortfolioTrade> RecordTradeAsync(RecordTradeInput input, CancellationToken ct = default)
    {
        var account = await _context.Accounts.FindAsync([input.AccountId], ct)
            ?? throw new InvalidOperationException($"Account {input.AccountId} not found");

        // Resolve symbol to ticker — find existing or create minimal entry
        var ticker = await _context.Tickers
            .FirstOrDefaultAsync(t => t.Symbol == input.Symbol, ct);

        if (ticker == null)
        {
            ticker = new Ticker
            {
                Symbol = input.Symbol,
                Name = input.Symbol,
                Market = "stocks",
                Active = true,
            };
            _context.Tickers.Add(ticker);
            await _context.SaveChangesAsync(ct);
        }

        // Create an auto-filled order for direct trade entry
        var order = new Order
        {
            Id = Guid.NewGuid(),
            AccountId = input.AccountId,
            TickerId = ticker.Id,
            Side = input.Side,
            OrderType = OrderType.Market,
            Quantity = input.Quantity,
            AssetType = input.AssetType,
            OptionContractId = input.OptionContractId,
            Status = OrderStatus.Filled,
            SubmittedAt = input.ExecutionTimestamp ?? DateTime.UtcNow,
            FilledAt = input.ExecutionTimestamp ?? DateTime.UtcNow,
        };

        _context.Orders.Add(order);

        var trade = new PortfolioTrade
        {
            Id = Guid.NewGuid(),
            AccountId = input.AccountId,
            OrderId = order.Id,
            TickerId = ticker.Id,
            Side = input.Side,
            Quantity = input.Quantity,
            Price = input.Price,
            Fees = input.Fees,
            AssetType = input.AssetType,
            OptionContractId = input.OptionContractId,
            Multiplier = input.Multiplier,
            ExecutionTimestamp = input.ExecutionTimestamp ?? DateTime.UtcNow,
        };

        _context.PortfolioTrades.Add(trade);

        // Create option leg if provided
        if (input.OptionLeg != null)
        {
            var leg = new OptionLeg
            {
                Id = Guid.NewGuid(),
                TradeId = trade.Id,
                OptionContractId = input.OptionLeg.OptionContractId,
                Quantity = input.OptionLeg.Quantity,
                EntryIV = input.OptionLeg.EntryIV,
                EntryDelta = input.OptionLeg.EntryDelta,
                EntryGamma = input.OptionLeg.EntryGamma,
                EntryTheta = input.OptionLeg.EntryTheta,
                EntryVega = input.OptionLeg.EntryVega,
            };
            _context.OptionLegs.Add(leg);
        }

        // Update cash
        var totalCost = input.Price * input.Quantity * input.Multiplier + input.Fees;
        if (input.Side == OrderSide.Buy)
            account.Cash -= totalCost;
        else
            account.Cash += input.Price * input.Quantity * input.Multiplier - input.Fees;

        await _context.SaveChangesAsync(ct);

        // Apply to position engine
        await _positionEngine.ApplyTradeAsync(trade, ct);

        _logger.LogInformation("[Portfolio] Trade recorded: {Side} {Qty} ticker {TickerId} @ {Price}",
            input.Side, input.Quantity, input.Symbol, input.Price);
        return trade;
    }

    public async Task<PortfolioState> GetPortfolioStateAsync(Guid accountId, CancellationToken ct = default)
    {
        var account = await _context.Accounts.FindAsync([accountId], ct)
            ?? throw new InvalidOperationException($"Account {accountId} not found");

        var positions = await _context.Positions
            .Include(p => p.Lots)
            .Include(p => p.Ticker)
            .Where(p => p.AccountId == accountId)
            .ToListAsync(ct);

        var recentTrades = await _context.PortfolioTrades
            .Include(t => t.Ticker)
            .Where(t => t.AccountId == accountId)
            .OrderByDescending(t => t.ExecutionTimestamp)
            .Take(20)
            .ToListAsync(ct);

        return new PortfolioState
        {
            Account = account,
            Positions = positions,
            RecentTrades = recentTrades,
            TotalRealizedPnL = positions.Sum(p => p.RealizedPnL),
        };
    }
}

using Backend.Data;
using Backend.Models.Portfolio;
using Backend.Services.Interfaces;
using Microsoft.EntityFrameworkCore;
using Microsoft.Extensions.Logging;

namespace Backend.Services.Implementation;

public class PositionEngine : IPositionEngine
{
    private readonly AppDbContext _context;
    private readonly ILogger<PositionEngine> _logger;

    public PositionEngine(AppDbContext context, ILogger<PositionEngine> logger)
    {
        _context = context;
        _logger = logger;
    }

    public async Task<List<Position>> RebuildPositionsAsync(Guid accountId, CancellationToken ct = default)
    {
        _logger.LogInformation("[STEP 1] Rebuilding positions for account {AccountId}", accountId);

        // Clear existing lots and positions for this account
        var existingLots = await _context.PositionLots
            .Where(l => l.Position.AccountId == accountId)
            .ToListAsync(ct);
        _context.PositionLots.RemoveRange(existingLots);

        var existingPositions = await _context.Positions
            .Where(p => p.AccountId == accountId)
            .ToListAsync(ct);
        _context.Positions.RemoveRange(existingPositions);

        await _context.SaveChangesAsync(ct);

        // Replay all trades in chronological order
        var trades = await _context.PortfolioTrades
            .Where(t => t.AccountId == accountId)
            .OrderBy(t => t.ExecutionTimestamp)
            .ToListAsync(ct);

        _logger.LogInformation("[STEP 2] Replaying {Count} trades", trades.Count);

        var positionIds = new HashSet<Guid>();
        foreach (var trade in trades)
        {
            var position = await ApplyTradeInternal(trade, ct);
            positionIds.Add(position.Id);
        }

        await _context.SaveChangesAsync(ct);

        // Return rebuilt positions with lots
        var positions = await _context.Positions
            .Include(p => p.Lots)
            .Where(p => positionIds.Contains(p.Id))
            .ToListAsync(ct);

        _logger.LogInformation("[STEP 3] Rebuild complete. {Count} positions created", positions.Count);
        return positions;
    }

    public async Task<Position> ApplyTradeAsync(PortfolioTrade trade, CancellationToken ct = default)
    {
        var position = await ApplyTradeInternal(trade, ct);
        await _context.SaveChangesAsync(ct);
        return position;
    }

    public decimal CalculateRealizedPnL(IEnumerable<PositionLot> lots)
    {
        return lots.Sum(l => l.RealizedPnL);
    }

    private async Task<Position> ApplyTradeInternal(PortfolioTrade trade, CancellationToken ct)
    {
        var position = await FindOrCreatePosition(trade, ct);

        if (trade.Side == OrderSide.Buy)
        {
            ApplyBuy(position, trade);
        }
        else
        {
            await ApplySellAsync(position, trade, ct);
        }

        RecalculatePosition(position, trade.ExecutionTimestamp);

        return position;
    }

    private async Task<Position> FindOrCreatePosition(PortfolioTrade trade, CancellationToken ct)
    {
        // Check local change tracker first to avoid re-querying tracked entities
        var position = _context.Positions.Local
            .FirstOrDefault(p => p.AccountId == trade.AccountId
                                 && p.TickerId == trade.TickerId
                                 && p.AssetType == trade.AssetType
                                 && p.Status == PositionStatus.Open
                                 && p.OptionContractId == trade.OptionContractId);

        if (position != null)
            return position;

        var query = _context.Positions
            .Include(p => p.Lots)
            .Where(p => p.AccountId == trade.AccountId
                        && p.TickerId == trade.TickerId
                        && p.AssetType == trade.AssetType
                        && p.Status == PositionStatus.Open);

        if (trade.OptionContractId.HasValue)
            query = query.Where(p => p.OptionContractId == trade.OptionContractId);
        else
            query = query.Where(p => p.OptionContractId == null);

        position = await query.FirstOrDefaultAsync(ct);

        if (position != null)
            return position;

        position = new Position
        {
            Id = Guid.NewGuid(),
            AccountId = trade.AccountId,
            TickerId = trade.TickerId,
            AssetType = trade.AssetType,
            OptionContractId = trade.OptionContractId,
            OpenedAt = trade.ExecutionTimestamp,
            LastUpdated = trade.ExecutionTimestamp,
        };

        _context.Positions.Add(position);
        return position;
    }

    private void ApplyBuy(Position position, PortfolioTrade trade)
    {
        var lot = new PositionLot
        {
            Id = Guid.NewGuid(),
            PositionId = position.Id,
            TradeId = trade.Id,
            Quantity = trade.Quantity,
            EntryPrice = trade.Price,
            RemainingQuantity = trade.Quantity,
            OpenedAt = trade.ExecutionTimestamp,
        };

        // Add to DbSet — EF relationship fix-up adds it to position.Lots automatically
        _context.PositionLots.Add(lot);
    }

    private async Task ApplySellAsync(Position position, PortfolioTrade trade, CancellationToken ct)
    {
        var remainingToClose = trade.Quantity;
        var multiplier = trade.Multiplier;

        // Query open lots from the store to ensure proper tracking
        var openLots = await _context.PositionLots
            .Where(l => l.PositionId == position.Id && l.RemainingQuantity > 0)
            .OrderBy(l => l.OpenedAt)
            .ToListAsync(ct);

        // Also check locally added lots not yet persisted
        var localLots = _context.PositionLots.Local
            .Where(l => l.PositionId == position.Id
                        && l.RemainingQuantity > 0
                        && !openLots.Any(ol => ol.Id == l.Id))
            .OrderBy(l => l.OpenedAt)
            .ToList();

        var allOpenLots = openLots.Concat(localLots).OrderBy(l => l.OpenedAt).ToList();

        foreach (var lot in allOpenLots)
        {
            if (remainingToClose <= 0) break;

            var closeQuantity = Math.Min(lot.RemainingQuantity, remainingToClose);
            var pnl = (trade.Price - lot.EntryPrice) * closeQuantity * multiplier;

            lot.RemainingQuantity -= closeQuantity;
            lot.RealizedPnL += pnl;

            if (lot.RemainingQuantity == 0)
                lot.ClosedAt = trade.ExecutionTimestamp;

            remainingToClose -= closeQuantity;
        }

        // Sync lots back to position navigation collection
        var allLots = await _context.PositionLots
            .Where(l => l.PositionId == position.Id)
            .ToListAsync(ct);
        var localOnlyLots = _context.PositionLots.Local
            .Where(l => l.PositionId == position.Id && !allLots.Any(al => al.Id == l.Id))
            .ToList();

        position.Lots.Clear();
        foreach (var lot in allLots.Concat(localOnlyLots))
            position.Lots.Add(lot);
    }

    private static void RecalculatePosition(Position position, DateTime timestamp)
    {
        var openLots = position.Lots.Where(l => l.RemainingQuantity > 0).ToList();

        position.NetQuantity = openLots.Sum(l => l.RemainingQuantity);
        position.RealizedPnL = position.Lots.Sum(l => l.RealizedPnL);
        position.LastUpdated = timestamp;

        if (openLots.Count > 0)
        {
            var totalCost = openLots.Sum(l => l.EntryPrice * l.RemainingQuantity);
            var totalQty = openLots.Sum(l => l.RemainingQuantity);
            position.AvgCostBasis = totalQty > 0 ? totalCost / totalQty : 0;
            position.Status = PositionStatus.Open;
            position.ClosedAt = null;
        }
        else
        {
            position.AvgCostBasis = 0;
            position.Status = PositionStatus.Closed;
            position.ClosedAt = timestamp;
        }
    }
}

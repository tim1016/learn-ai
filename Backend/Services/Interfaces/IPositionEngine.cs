using Backend.Models.Portfolio;

namespace Backend.Services.Interfaces;

public interface IPositionEngine
{
    Task<List<Position>> RebuildPositionsAsync(Guid accountId, CancellationToken ct = default);
    Task<Position> ApplyTradeAsync(PortfolioTrade trade, CancellationToken ct = default);
    decimal CalculateRealizedPnL(IEnumerable<PositionLot> lots);
}

using Backend.Data;
using Backend.Models.Portfolio;
using Backend.Services.Interfaces;
using Microsoft.EntityFrameworkCore;
using Microsoft.Extensions.Logging;

namespace Backend.Services.Implementation;

public class PortfolioValuationService : IPortfolioValuationService
{
    private readonly AppDbContext _context;
    private readonly IPolygonService _polygonService;
    private readonly ILogger<PortfolioValuationService> _logger;

    public PortfolioValuationService(AppDbContext context, IPolygonService polygonService,
        ILogger<PortfolioValuationService> logger)
    {
        _context = context;
        _polygonService = polygonService;
        _logger = logger;
    }

    public async Task<PortfolioValuation> ComputeValuationAsync(Guid accountId, CancellationToken ct = default)
    {
        var account = await _context.Accounts.FindAsync([accountId], ct)
            ?? throw new InvalidOperationException($"Account {accountId} not found");

        var positions = await _context.Positions
            .Include(p => p.Ticker)
            .Include(p => p.Lots)
            .Include(p => p.OptionContract)
            .Where(p => p.AccountId == accountId && p.Status == PositionStatus.Open)
            .ToListAsync(ct);

        // Fetch live prices for all unique tickers
        var symbols = positions.Select(p => p.Ticker.Symbol).Distinct().ToList();
        var prices = await FetchLivePricesAsync(symbols, ct);

        return ComputeValuationInternal(account, positions, prices);
    }

    public async Task<PortfolioValuation> ComputeValuationWithPricesAsync(Guid accountId,
        Dictionary<string, decimal> prices, CancellationToken ct = default)
    {
        var account = await _context.Accounts.FindAsync([accountId], ct)
            ?? throw new InvalidOperationException($"Account {accountId} not found");

        var positions = await _context.Positions
            .Include(p => p.Ticker)
            .Include(p => p.Lots)
            .Include(p => p.OptionContract)
            .Where(p => p.AccountId == accountId && p.Status == PositionStatus.Open)
            .ToListAsync(ct);

        return ComputeValuationInternal(account, positions, prices);
    }

    private PortfolioValuation ComputeValuationInternal(Account account, List<Position> positions,
        Dictionary<string, decimal> prices)
    {
        var positionValuations = new List<PositionValuation>();
        decimal totalMarketValue = 0;
        decimal totalUnrealizedPnL = 0;
        decimal totalRealizedPnL = 0;
        decimal? netDelta = null, netGamma = null, netTheta = null, netVega = null;

        foreach (var position in positions)
        {
            var symbol = position.Ticker.Symbol;
            if (!prices.TryGetValue(symbol, out var currentPrice))
            {
                _logger.LogWarning("[Valuation] No price found for {Symbol}, skipping position", symbol);
                continue;
            }

            var multiplier = position.OptionContractId.HasValue
                ? (position.OptionContract?.Multiplier ?? 100)
                : 1;

            var marketValue = currentPrice * position.NetQuantity * multiplier;
            var costBasis = position.AvgCostBasis * position.NetQuantity * multiplier;
            var unrealizedPnL = marketValue - costBasis;

            positionValuations.Add(new PositionValuation
            {
                PositionId = position.Id,
                Symbol = symbol,
                Quantity = position.NetQuantity,
                CostBasis = costBasis,
                CurrentPrice = currentPrice,
                MarketValue = marketValue,
                UnrealizedPnL = unrealizedPnL,
                Multiplier = multiplier,
            });

            totalMarketValue += marketValue;
            totalUnrealizedPnL += unrealizedPnL;
            totalRealizedPnL += position.RealizedPnL;

            // Aggregate Greeks from option legs if available
            if (position.AssetType == AssetType.Option)
            {
                var legs = _context.OptionLegs
                    .Where(l => l.Trade.AccountId == account.Id
                                && l.OptionContractId == position.OptionContractId)
                    .OrderByDescending(l => l.Trade.ExecutionTimestamp)
                    .FirstOrDefault();

                if (legs != null)
                {
                    netDelta = (netDelta ?? 0) + (legs.EntryDelta ?? 0) * position.NetQuantity * multiplier;
                    netGamma = (netGamma ?? 0) + (legs.EntryGamma ?? 0) * position.NetQuantity * multiplier;
                    netTheta = (netTheta ?? 0) + (legs.EntryTheta ?? 0) * position.NetQuantity * multiplier;
                    netVega = (netVega ?? 0) + (legs.EntryVega ?? 0) * position.NetQuantity * multiplier;
                }
            }
        }

        return new PortfolioValuation
        {
            Cash = account.Cash,
            MarketValue = totalMarketValue,
            Equity = account.Cash + totalMarketValue,
            UnrealizedPnL = totalUnrealizedPnL,
            RealizedPnL = totalRealizedPnL,
            NetDelta = netDelta,
            NetGamma = netGamma,
            NetTheta = netTheta,
            NetVega = netVega,
            Positions = positionValuations,
        };
    }

    private async Task<Dictionary<string, decimal>> FetchLivePricesAsync(
        List<string> symbols, CancellationToken ct)
    {
        var prices = new Dictionary<string, decimal>(StringComparer.OrdinalIgnoreCase);
        if (symbols.Count == 0) return prices;

        try
        {
            var response = await _polygonService.FetchStockSnapshotsAsync(symbols, ct);
            foreach (var snapshot in response.Snapshots)
            {
                if (snapshot.Ticker != null && snapshot.Day?.Close != null)
                    prices[snapshot.Ticker] = snapshot.Day.Close.Value;
                else if (snapshot.Ticker != null && snapshot.PrevDay?.Close != null)
                    prices[snapshot.Ticker] = snapshot.PrevDay.Close.Value;
            }
        }
        catch (Exception ex)
        {
            _logger.LogWarning(ex, "[Valuation] Failed to fetch live prices, using empty price map");
        }

        return prices;
    }
}

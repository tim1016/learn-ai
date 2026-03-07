using Backend.Data;
using Backend.Models.Portfolio;
using Backend.Services.Interfaces;
using Microsoft.EntityFrameworkCore;
using Microsoft.Extensions.Logging;

namespace Backend.Services.Implementation;

public class SnapshotService : ISnapshotService
{
    private readonly AppDbContext _context;
    private readonly IPortfolioValuationService _valuationService;
    private readonly ILogger<SnapshotService> _logger;

    public SnapshotService(AppDbContext context, IPortfolioValuationService valuationService,
        ILogger<SnapshotService> logger)
    {
        _context = context;
        _valuationService = valuationService;
        _logger = logger;
    }

    public async Task<PortfolioSnapshot> TakeSnapshotAsync(Guid accountId, CancellationToken ct = default)
    {
        var valuation = await _valuationService.ComputeValuationAsync(accountId, ct);
        return await PersistSnapshot(accountId, valuation, ct);
    }

    public async Task<PortfolioSnapshot> TakeSnapshotWithPricesAsync(Guid accountId,
        Dictionary<string, decimal> prices, CancellationToken ct = default)
    {
        var valuation = await _valuationService.ComputeValuationWithPricesAsync(accountId, prices, ct);
        return await PersistSnapshot(accountId, valuation, ct);
    }

    public async Task<List<PortfolioSnapshot>> GetEquityCurveAsync(Guid accountId,
        DateTime? from = null, DateTime? to = null, CancellationToken ct = default)
    {
        var query = _context.PortfolioSnapshots
            .Where(s => s.AccountId == accountId)
            .OrderBy(s => s.Timestamp);

        if (from.HasValue)
            query = (IOrderedQueryable<PortfolioSnapshot>)query.Where(s => s.Timestamp >= from.Value);
        if (to.HasValue)
            query = (IOrderedQueryable<PortfolioSnapshot>)query.Where(s => s.Timestamp <= to.Value);

        return await query.ToListAsync(ct);
    }

    public async Task<List<DrawdownPoint>> GetDrawdownSeriesAsync(Guid accountId, CancellationToken ct = default)
    {
        var snapshots = await _context.PortfolioSnapshots
            .Where(s => s.AccountId == accountId)
            .OrderBy(s => s.Timestamp)
            .Select(s => new { s.Timestamp, s.Equity })
            .ToListAsync(ct);

        return ComputeDrawdownSeries(snapshots.Select(s => (s.Timestamp, s.Equity)).ToList());
    }

    public PortfolioMetrics ComputeMetrics(List<PortfolioSnapshot> snapshots)
    {
        if (snapshots.Count < 2)
        {
            return new PortfolioMetrics { SnapshotCount = snapshots.Count };
        }

        var ordered = snapshots.OrderBy(s => s.Timestamp).ToList();

        // Compute daily returns
        var returns = new List<decimal>();
        for (int i = 1; i < ordered.Count; i++)
        {
            if (ordered[i - 1].Equity != 0)
                returns.Add((ordered[i].Equity - ordered[i - 1].Equity) / ordered[i - 1].Equity);
        }

        if (returns.Count == 0)
            return new PortfolioMetrics { SnapshotCount = snapshots.Count };

        var meanReturn = returns.Average();
        var stdDev = ComputeStdDev(returns);

        // Sharpe Ratio (annualized, assuming daily snapshots)
        var sharpe = stdDev != 0 ? meanReturn / stdDev * (decimal)Math.Sqrt(252) : 0;

        // Sortino Ratio (downside deviation only)
        var downsideReturns = returns.Where(r => r < 0).ToList();
        var downsideDev = downsideReturns.Count > 0 ? ComputeStdDev(downsideReturns) : 0;
        var sortino = downsideDev != 0 ? meanReturn / downsideDev * (decimal)Math.Sqrt(252) : 0;

        // Max Drawdown
        var drawdownSeries = ComputeDrawdownSeries(
            ordered.Select(s => (s.Timestamp, s.Equity)).ToList());
        var maxDrawdown = drawdownSeries.Count > 0
            ? drawdownSeries.Max(d => d.Drawdown)
            : 0;
        var maxDrawdownPercent = drawdownSeries.Count > 0
            ? drawdownSeries.Max(d => d.DrawdownPercent)
            : 0;

        // Calmar Ratio (annualized return / max drawdown)
        var totalReturn = ordered.Last().Equity - ordered.First().Equity;
        var totalReturnPercent = ordered.First().Equity != 0
            ? totalReturn / ordered.First().Equity
            : 0;
        var annualizedReturn = meanReturn * 252;
        var calmar = maxDrawdownPercent != 0 ? annualizedReturn / maxDrawdownPercent : 0;

        // Win Rate and Profit Factor
        var positiveReturns = returns.Where(r => r > 0).ToList();
        var negativeReturns = returns.Where(r => r < 0).ToList();
        var winRate = returns.Count > 0 ? (decimal)positiveReturns.Count / returns.Count : 0;
        var grossProfit = positiveReturns.Sum();
        var grossLoss = Math.Abs(negativeReturns.Sum());
        var profitFactor = grossLoss != 0 ? grossProfit / grossLoss : grossProfit > 0 ? decimal.MaxValue : 0;

        return new PortfolioMetrics
        {
            SharpeRatio = Math.Round(sharpe, 4),
            SortinoRatio = Math.Round(sortino, 4),
            MaxDrawdown = Math.Round(maxDrawdown, 8),
            MaxDrawdownPercent = Math.Round(maxDrawdownPercent, 4),
            CalmarRatio = Math.Round(calmar, 4),
            WinRate = Math.Round(winRate, 4),
            ProfitFactor = Math.Round(profitFactor, 4),
            TotalReturn = Math.Round(totalReturn, 8),
            TotalReturnPercent = Math.Round(totalReturnPercent, 4),
            SnapshotCount = snapshots.Count,
        };
    }

    private async Task<PortfolioSnapshot> PersistSnapshot(Guid accountId,
        PortfolioValuation valuation, CancellationToken ct)
    {
        var snapshot = new PortfolioSnapshot
        {
            Id = Guid.NewGuid(),
            AccountId = accountId,
            Timestamp = DateTime.UtcNow,
            Equity = valuation.Equity,
            Cash = valuation.Cash,
            MarketValue = valuation.MarketValue,
            MarginUsed = 0,
            UnrealizedPnL = valuation.UnrealizedPnL,
            RealizedPnL = valuation.RealizedPnL,
            NetDelta = valuation.NetDelta,
            NetGamma = valuation.NetGamma,
            NetTheta = valuation.NetTheta,
            NetVega = valuation.NetVega,
        };

        _context.PortfolioSnapshots.Add(snapshot);
        await _context.SaveChangesAsync(ct);

        _logger.LogInformation("[Snapshot] Captured for account {AccountId}: Equity={Equity:C}, MV={MV:C}",
            accountId, valuation.Equity, valuation.MarketValue);

        return snapshot;
    }

    private static List<DrawdownPoint> ComputeDrawdownSeries(List<(DateTime Timestamp, decimal Equity)> data)
    {
        var result = new List<DrawdownPoint>();
        decimal peak = 0;

        foreach (var (timestamp, equity) in data)
        {
            if (equity > peak) peak = equity;

            var drawdown = peak - equity;
            var drawdownPercent = peak > 0 ? drawdown / peak : 0;

            result.Add(new DrawdownPoint
            {
                Timestamp = timestamp,
                Equity = equity,
                PeakEquity = peak,
                Drawdown = drawdown,
                DrawdownPercent = drawdownPercent,
            });
        }

        return result;
    }

    private static decimal ComputeStdDev(List<decimal> values)
    {
        if (values.Count < 2) return 0;

        var mean = values.Average();
        var sumSquaredDiffs = values.Sum(v => (v - mean) * (v - mean));
        return (decimal)Math.Sqrt((double)(sumSquaredDiffs / (values.Count - 1)));
    }
}

using Backend.Models.Portfolio;

namespace Backend.Services.Interfaces;

public interface ISnapshotService
{
    Task<PortfolioSnapshot> TakeSnapshotAsync(Guid accountId, CancellationToken ct = default);
    Task<PortfolioSnapshot> TakeSnapshotWithPricesAsync(Guid accountId,
        Dictionary<string, decimal> prices, CancellationToken ct = default);
    Task<List<PortfolioSnapshot>> GetEquityCurveAsync(Guid accountId,
        DateTime? from = null, DateTime? to = null, CancellationToken ct = default);
    Task<List<DrawdownPoint>> GetDrawdownSeriesAsync(Guid accountId, CancellationToken ct = default);
    PortfolioMetrics ComputeMetrics(List<PortfolioSnapshot> snapshots);
}

public class DrawdownPoint
{
    public DateTime Timestamp { get; set; }
    public decimal Equity { get; set; }
    public decimal PeakEquity { get; set; }
    public decimal Drawdown { get; set; }
    public decimal DrawdownPercent { get; set; }
}

public class PortfolioMetrics
{
    public decimal SharpeRatio { get; set; }
    public decimal SortinoRatio { get; set; }
    public decimal MaxDrawdown { get; set; }
    public decimal MaxDrawdownPercent { get; set; }
    public decimal CalmarRatio { get; set; }
    public decimal WinRate { get; set; }
    public decimal ProfitFactor { get; set; }
    public decimal TotalReturn { get; set; }
    public decimal TotalReturnPercent { get; set; }
    public int SnapshotCount { get; set; }
}

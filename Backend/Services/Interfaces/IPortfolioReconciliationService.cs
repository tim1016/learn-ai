namespace Backend.Services.Interfaces;

public interface IPortfolioReconciliationService
{
    Task<ReconciliationReport> ReconcileAsync(Guid accountId, CancellationToken ct = default);
    Task AutoFixAsync(Guid accountId, CancellationToken ct = default);
}

public class ReconciliationReport
{
    public Guid AccountId { get; set; }
    public bool HasDrift { get; set; }
    public List<PositionDrift> Drifts { get; set; } = [];
    public int CachedPositionCount { get; set; }
    public int RebuiltPositionCount { get; set; }
}

public class PositionDrift
{
    public int TickerId { get; set; }
    public string Symbol { get; set; } = "";
    public decimal CachedQuantity { get; set; }
    public decimal RebuiltQuantity { get; set; }
    public decimal CachedRealizedPnL { get; set; }
    public decimal RebuiltRealizedPnL { get; set; }
    public string DriftType { get; set; } = "";
}

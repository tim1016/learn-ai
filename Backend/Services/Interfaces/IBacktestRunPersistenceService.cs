using Backend.Models.MarketData;

namespace Backend.Services.Interfaces;

public interface IBacktestRunPersistenceService
{
    Task<int> PersistAsync(PersistLeanRunPayload payload, CancellationToken ct);
}

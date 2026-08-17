using Backend.Models.DTOs;

namespace Backend.Services.Interfaces;

public record RecencyPersistResult(int? RecencyRunId, bool Skipped);

public interface IRecencyPersistenceService
{
    Task<RecencyPersistResult> PersistSnapshotAsync(RecencySnapshotRequest request, CancellationToken ct);
}

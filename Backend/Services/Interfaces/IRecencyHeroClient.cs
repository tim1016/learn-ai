using Backend.Models.DTOs;

namespace Backend.Services.Interfaces;

public interface IRecencyHeroClient
{
    Task<IReadOnlyList<RecencyHeroResponseItem>> SelectHeroesAsync(
        RecencyHeroRequest request,
        CancellationToken ct);
}

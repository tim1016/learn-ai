using Backend.Models.Comparison;

namespace Backend.Services.Interfaces;

public interface IComparisonService
{
    Task<CompareTradesResponse> CompareTradesAsync(
        CompareTradesRequest request,
        CancellationToken ct = default);
}

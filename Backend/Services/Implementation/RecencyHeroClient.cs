using System.Net.Http.Json;
using System.Text.Json;
using Backend.Models.DTOs;
using Backend.Services.Interfaces;

namespace Backend.Services.Implementation;

public sealed class RecencyHeroClient : IRecencyHeroClient
{
    private static readonly JsonSerializerOptions JsonOptions = new()
    {
        PropertyNamingPolicy = JsonNamingPolicy.SnakeCaseLower,
        PropertyNameCaseInsensitive = true,
    };

    private readonly HttpClient _http;

    public RecencyHeroClient(HttpClient http)
    {
        _http = http;
    }

    public async Task<IReadOnlyList<RecencyHeroResponseItem>> SelectHeroesAsync(
        RecencyHeroRequest request,
        CancellationToken ct)
    {
        using var response = await _http.PostAsJsonAsync("/api/research/recency/hero", request, JsonOptions, ct);
        response.EnsureSuccessStatusCode();
        var body = await response.Content.ReadFromJsonAsync<RecencyHeroResponse>(JsonOptions, ct);
        return body?.Heroes ?? [];
    }
}

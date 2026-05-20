using System.Text.Json;
using System.Text.Json.Serialization;

namespace Backend.GraphQL.Types;

/// <summary>
/// PR B (2026-05-19) — canonical DataPolicy block exposed on
/// <c>BacktestRun.dataPolicy</c>. Mirrors the Python
/// ``app.lean_sidecar.data_policy.DataPolicy`` dataclass exactly; the field
/// layout below matches the persisted JSON shape in
/// <see cref="Backend.Models.MarketData.StrategyExecution.DataPolicyJson"/>.
///
/// See <c>docs/superpowers/specs/2026-05-19-pr-b-engine-lab-unified-design.md</c>
/// § 6.1 for the canonical example and § 6.6 for the GraphQL schema slice.
/// </summary>
public sealed record DataPolicyType(
    string Source,
    string Symbol,
    bool Adjusted,
    string Session,
    BarsSpecType InputBars,
    BarsSpecType StrategyBars,
    string TimestampPolicy,
    string Timezone,
    string ProviderKind,
    string? FixtureId,
    string? FixtureSha256)
{
    private static readonly JsonSerializerOptions _options = new()
    {
        PropertyNamingPolicy = JsonNamingPolicy.SnakeCaseLower,
        PropertyNameCaseInsensitive = true,
    };

    /// <summary>
    /// Parse the persisted <see cref="Backend.Models.MarketData.StrategyExecution.DataPolicyJson"/>
    /// string into a <see cref="DataPolicyType"/>. Returns <c>null</c> on legacy rows
    /// (null/empty input) and on any deserialization failure — the field is intentionally
    /// nullable on the GraphQL surface so corrupt rows surface as missing rather than
    /// erroring the whole query.
    /// </summary>
    public static DataPolicyType? TryParse(string? json)
    {
        if (string.IsNullOrWhiteSpace(json))
            return null;

        try
        {
            var dto = JsonSerializer.Deserialize<DataPolicyJsonDto>(json, _options);
            if (dto is null) return null;
            return new DataPolicyType(
                Source: dto.Source ?? "",
                Symbol: dto.Symbol ?? "",
                Adjusted: dto.Adjusted,
                Session: dto.Session ?? "",
                InputBars: new BarsSpecType(dto.InputBars?.Timespan ?? "", dto.InputBars?.Multiplier ?? 0),
                StrategyBars: new BarsSpecType(dto.StrategyBars?.Timespan ?? "", dto.StrategyBars?.Multiplier ?? 0),
                TimestampPolicy: dto.TimestampPolicy ?? "",
                Timezone: dto.Timezone ?? "",
                ProviderKind: dto.ProviderKind ?? "",
                FixtureId: dto.FixtureId,
                FixtureSha256: dto.FixtureSha256);
        }
        catch (JsonException)
        {
            return null;
        }
    }

    /// <summary>DTO used only for JSON deserialization (matches the snake_case wire shape).</summary>
    private sealed class DataPolicyJsonDto
    {
        [JsonPropertyName("source")] public string? Source { get; set; }
        [JsonPropertyName("symbol")] public string? Symbol { get; set; }
        [JsonPropertyName("adjusted")] public bool Adjusted { get; set; }
        [JsonPropertyName("session")] public string? Session { get; set; }
        [JsonPropertyName("input_bars")] public BarsSpecJsonDto? InputBars { get; set; }
        [JsonPropertyName("strategy_bars")] public BarsSpecJsonDto? StrategyBars { get; set; }
        [JsonPropertyName("timestamp_policy")] public string? TimestampPolicy { get; set; }
        [JsonPropertyName("timezone")] public string? Timezone { get; set; }
        [JsonPropertyName("provider_kind")] public string? ProviderKind { get; set; }
        [JsonPropertyName("fixture_id")] public string? FixtureId { get; set; }
        [JsonPropertyName("fixture_sha256")] public string? FixtureSha256 { get; set; }
    }

    private sealed class BarsSpecJsonDto
    {
        [JsonPropertyName("timespan")] public string? Timespan { get; set; }
        [JsonPropertyName("multiplier")] public int Multiplier { get; set; }
    }
}

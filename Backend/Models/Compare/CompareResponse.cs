using System.Text.Json.Serialization;
using Backend.Services;

namespace Backend.Models.Compare;

/// <summary>
/// PR B (2026-05-19) Phase 4 — wire shape for ``GET /api/runs/compare``.
/// Mirrors spec § 6.5 verbatim; field names are snake_case on the wire so
/// the Angular ``RunsCompareService`` and the upstream Python service share
/// one canonical shape across all three layers.
/// </summary>
public record CompareResponse(
    [property: JsonPropertyName("left")] RunSummary Left,
    [property: JsonPropertyName("right")] RunSummary Right,
    [property: JsonPropertyName("compatible")] bool Compatible,
    [property: JsonPropertyName("mismatches")] List<string> Mismatches,
    [property: JsonPropertyName("informational_differences")] List<string> InformationalDifferences,
    [property: JsonPropertyName("summary_deltas")] SummaryDeltas SummaryDeltas,
    [property: JsonPropertyName("trade_diff")] TradeDiff TradeDiff,
    [property: JsonPropertyName("first_divergence")] TradeDivergence? FirstDivergence,
    [property: JsonPropertyName("state_trace_available")] bool StateTraceAvailable,
    [property: JsonPropertyName("raw_run_links")] RawRunLinks RawRunLinks);

/// <summary>
/// PR B Phase 4 — one side of the compare. Wire shape mirrors spec § 6.5
/// (snake_case). ``DataPolicy`` is parsed from the persisted JSON blob and
/// is null on legacy rows that pre-date PR B Phase 2.
/// </summary>
public record RunSummary(
    [property: JsonPropertyName("id")] int Id,
    [property: JsonPropertyName("engine")] string Engine,
    [property: JsonPropertyName("data_policy")] DataPolicyDto? DataPolicy,
    [property: JsonPropertyName("summary")] RunSummaryStats Summary,
    [property: JsonPropertyName("starting_cash")] decimal StartingCash,
    [property: JsonPropertyName("commission_per_order")] string CommissionPerOrder,
    [property: JsonPropertyName("fill_mode")] string FillMode,
    [property: JsonPropertyName("brokerage_policy")] string? BrokeragePolicy,
    [property: JsonPropertyName("strategy_identity")] StrategyIdentity StrategyIdentity);

public record DataPolicyDto(
    [property: JsonPropertyName("source")] string Source,
    [property: JsonPropertyName("symbol")] string Symbol,
    [property: JsonPropertyName("adjusted")] bool Adjusted,
    [property: JsonPropertyName("session")] string Session,
    [property: JsonPropertyName("input_bars")] BarsSpecDto InputBars,
    [property: JsonPropertyName("strategy_bars")] BarsSpecDto StrategyBars,
    [property: JsonPropertyName("timestamp_policy")] string TimestampPolicy,
    [property: JsonPropertyName("timezone")] string Timezone,
    [property: JsonPropertyName("provider_kind")] string ProviderKind,
    [property: JsonPropertyName("fixture_id")] string? FixtureId,
    [property: JsonPropertyName("fixture_sha256")] string? FixtureSha256);

public record BarsSpecDto(
    [property: JsonPropertyName("timespan")] string Timespan,
    [property: JsonPropertyName("multiplier")] int Multiplier);

/// <summary>
/// PR B Phase 4 — flattened run stats surfaced on the compare-view
/// Summary cards. Mirrors spec § 6.5's ``summary`` block; the per-metric
/// <see cref="SummaryDeltas"/> bundle on the parent <see cref="CompareResponse"/>
/// carries the per-metric ``left/right/delta`` triple separately so the UI
/// can render either raw stats or deltas without re-reading the trade
/// table.
/// </summary>
public record RunSummaryStats(
    [property: JsonPropertyName("total_trades")] int TotalTrades,
    [property: JsonPropertyName("total_pnl")] decimal TotalPnL,
    [property: JsonPropertyName("total_fees")] decimal TotalFees,
    [property: JsonPropertyName("win_rate")] decimal WinRate,
    [property: JsonPropertyName("max_drawdown")] decimal MaxDrawdown,
    [property: JsonPropertyName("sharpe")] decimal Sharpe);

public record StrategyIdentity(
    [property: JsonPropertyName("kind")] string Kind,
    [property: JsonPropertyName("name")] string Name,
    [property: JsonPropertyName("sha256")] string? Sha256);

public record RawRunLinks(
    [property: JsonPropertyName("left")] RawRunSide Left,
    [property: JsonPropertyName("right")] RawRunSide Right);

public record RawRunSide(
    [property: JsonPropertyName("manifest_path")] string? ManifestPath,
    [property: JsonPropertyName("log_path")] string? LogPath,
    [property: JsonPropertyName("staged_zip_sha256")] Dictionary<string, string>? StagedZipSha256);

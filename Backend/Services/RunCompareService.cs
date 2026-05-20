using System.Text.Json;
using System.Text.Json.Serialization;
using Backend.Models.MarketData;

namespace Backend.Services;

/// <summary>
/// PR B (2026-05-19) Phase 4 — compare-view domain service. Composes the
/// equivalence gate, the summary-delta computation, and state-trace
/// availability detection for two <see cref="StrategyExecution"/> rows.
/// Trade reconciliation is delegated to the Python ``reconcile_trade_lists``
/// endpoint via <see cref="ReconcileTrades"/>; this class never imports the
/// Python algorithm itself, only the wire shape.
///
/// Gate strictness rules (see spec § 9.1–9.3):
/// <list type="bullet">
///   <item><description><b>Gate-strict</b> — every DataPolicy field
///   (symbol, session, adjusted, input_bars, strategy_bars),
///   <c>starting_cash</c>, <c>commission_per_order</c>, <c>fill_mode</c>,
///   and the run window (<c>start_date</c> / <c>end_date</c>).</description></item>
///   <item><description><b>Soft / informational</b> — <c>brokerage_policy</c>
///   when either side is null or <c>"algorithm_default"</c>; gates only when
///   both sides declare a non-default value and they differ.</description></item>
///   <item><description><b>Hard failure (no comparison possible)</b> —
///   either side missing <c>data_policy_json</c> emits the synthetic
///   <c>data_policy_missing</c> mismatch token.</description></item>
/// </list>
/// </summary>
public class RunCompareService
{
    public CompatibilityResult EvaluateCompatibility(StrategyExecution left, StrategyExecution right)
    {
        var mismatches = new List<string>();
        var infos = new List<string>();

        var leftDp = ParseDataPolicy(left.DataPolicyJson);
        var rightDp = ParseDataPolicy(right.DataPolicyJson);

        if (leftDp is null || rightDp is null)
        {
            mismatches.Add("data_policy_missing");
            return new CompatibilityResult { Compatible = false, Mismatches = mismatches };
        }

        // Gate-strict DataPolicy fields.
        if (!string.Equals(leftDp.Symbol, rightDp.Symbol, StringComparison.OrdinalIgnoreCase))
        {
            mismatches.Add("symbol");
        }

        if (leftDp.Session != rightDp.Session)
        {
            mismatches.Add("session");
        }

        if (leftDp.Adjusted != rightDp.Adjusted)
        {
            mismatches.Add("adjusted");
        }

        if (!BarsSpecEquals(leftDp.InputBars, rightDp.InputBars))
        {
            mismatches.Add("input_bars");
        }

        if (!BarsSpecEquals(leftDp.StrategyBars, rightDp.StrategyBars))
        {
            mismatches.Add("strategy_bars");
        }

        // Gate-strict run-parameter fields (live on StrategyExecution, not in
        // the DataPolicy block).
        if (left.StartDate != right.StartDate || left.EndDate != right.EndDate)
        {
            mismatches.Add("window");
        }

        if (left.InitialCash != right.InitialCash)
        {
            mismatches.Add("starting_cash");
        }

        if ((left.CommissionPerOrder ?? 0m) != (right.CommissionPerOrder ?? 0m))
        {
            mismatches.Add("commission_per_order");
        }

        if (left.FillMode != right.FillMode)
        {
            mismatches.Add("fill_mode");
        }

        // Soft / informational gate — brokerage policy.
        // When either side is null or "algorithm_default", treat as
        // informational difference (not a gate failure).  Only fail when
        // both sides declare a non-default value and the two values differ.
        var leftSoft = IsSoftBrokerage(left.BrokeragePolicy);
        var rightSoft = IsSoftBrokerage(right.BrokeragePolicy);
        if (left.BrokeragePolicy != right.BrokeragePolicy)
        {
            if (leftSoft || rightSoft)
            {
                infos.Add("brokerage_policy");
            }
            else
            {
                mismatches.Add("brokerage_policy");
            }
        }

        return new CompatibilityResult
        {
            Compatible = mismatches.Count == 0,
            Mismatches = mismatches,
            InformationalDifferences = infos,
        };
    }

    // -------------------------------------------------------------------
    // Private helpers
    // -------------------------------------------------------------------

    private static readonly JsonSerializerOptions _dataPolicyOpts = new()
    {
        PropertyNameCaseInsensitive = true,
    };

    private static DataPolicySnapshot? ParseDataPolicy(string? json)
    {
        if (string.IsNullOrWhiteSpace(json))
        {
            return null;
        }

        try
        {
            return JsonSerializer.Deserialize<DataPolicySnapshot>(json, _dataPolicyOpts);
        }
        catch (JsonException)
        {
            return null;
        }
    }

    private static bool BarsSpecEquals(BarsSpecSnapshot? a, BarsSpecSnapshot? b)
    {
        if (a is null && b is null)
        {
            return true;
        }
        if (a is null || b is null)
        {
            return false;
        }
        return string.Equals(a.Timespan, b.Timespan, StringComparison.Ordinal)
            && a.Multiplier == b.Multiplier;
    }

    private static bool IsSoftBrokerage(string? policy)
    {
        return string.IsNullOrEmpty(policy) || policy == "algorithm_default";
    }

    // -------------------------------------------------------------------
    // Internal DTOs for DataPolicy JSON parsing
    // -------------------------------------------------------------------

    private sealed class DataPolicySnapshot
    {
        [JsonPropertyName("source")] public string? Source { get; set; }
        [JsonPropertyName("symbol")] public string? Symbol { get; set; }
        [JsonPropertyName("adjusted")] public bool Adjusted { get; set; }
        [JsonPropertyName("session")] public string? Session { get; set; }
        [JsonPropertyName("input_bars")] public BarsSpecSnapshot? InputBars { get; set; }
        [JsonPropertyName("strategy_bars")] public BarsSpecSnapshot? StrategyBars { get; set; }
        [JsonPropertyName("timestamp_policy")] public string? TimestampPolicy { get; set; }
        [JsonPropertyName("timezone")] public string? Timezone { get; set; }
        [JsonPropertyName("provider_kind")] public string? ProviderKind { get; set; }
        [JsonPropertyName("fixture_id")] public string? FixtureId { get; set; }
        [JsonPropertyName("fixture_sha256")] public string? FixtureSha256 { get; set; }
    }

    private sealed class BarsSpecSnapshot
    {
        [JsonPropertyName("timespan")] public string? Timespan { get; set; }
        [JsonPropertyName("multiplier")] public int Multiplier { get; set; }
    }
}

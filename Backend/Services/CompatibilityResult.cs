namespace Backend.Services;

/// <summary>
/// PR B (2026-05-19) Phase 4 — output of
/// <see cref="RunCompareService.EvaluateCompatibility"/>. Carries the boolean
/// verdict plus the two field lists the compare-view header renders:
/// <list type="bullet">
///   <item><description><c>Mismatches</c> — gate-strict fields that disagree.
///   Populated only when <c>Compatible == false</c>.</description></item>
///   <item><description><c>InformationalDifferences</c> — fields that
///   disagree but are intentionally soft-gated (e.g. <c>brokerage_policy</c>
///   when either side is <c>"algorithm_default"</c> / null). Surfaced in the
///   UI as a note, never a failure.</description></item>
/// </list>
/// See spec § 9.1–9.3 for the full field taxonomy.
/// </summary>
public class CompatibilityResult
{
    public bool Compatible { get; init; }
    public List<string> Mismatches { get; init; } = new();
    public List<string> InformationalDifferences { get; init; } = new();
}

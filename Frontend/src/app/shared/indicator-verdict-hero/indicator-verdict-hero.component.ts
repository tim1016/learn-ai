import {
  ChangeDetectionStrategy,
  Component,
  computed,
  input,
  output,
} from "@angular/core";
import { CommonModule } from "@angular/common";

import {
  type Verdict,
  type VerdictAnalysis,
  type VerdictTier,
  computeVerdict,
  verdictHeadline,
} from "./indicator-verdict-hero.types";

export type VerdictCta =
  | "send-to-preflight"
  | "save-verdict"
  | "run-on-another-ticker";

/**
 * Verdict hero for the Indicator Reliability page.
 *
 * Three-column band at the top of the analysis:
 *   1. Screens-passed gauge (N/5 + tier label)
 *   2. Headline verdict + reason pills + blocker bar
 *   3. Three big-action CTAs
 *
 * Below that: a 3-cell WHEN / WHERE / HOW decision band.
 *
 * v2 review changes:
 * - Replaced 0-100 "confidence" scalar with screens-passed count (5/5).
 *   The framework explicitly rejects a single reliability score.
 * - Renamed tier "trade" → "pre_flight" since this page does not
 *   validate live trading; the right gate is the Strategy Lab
 *   pre-flight pipeline.
 * - "Economically meaningful" pill renamed to "|IC| > 0.10 magnitude"
 *   since the page tests statistical magnitude, not cost-net economics.
 * - Hard demotions (single-asset only / Sharpe proxy < 0 / shuffle
 *   count < 1000) prevent reaching pre-flight tier and surface as
 *   blockers below the headline so the reader can see why a 5/5 result
 *   is still not graduating.
 *
 * The hero does not fetch anything — the host component passes in a
 * pre-computed ``VerdictAnalysis`` bundle. That keeps the Research Lab
 * page the single owner of the analysis response shape and lets this
 * component be reused elsewhere (e.g. Strategy Lab pre-flight).
 */
@Component({
  selector: "app-indicator-verdict-hero",
  imports: [CommonModule],
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: "./indicator-verdict-hero.component.html",
  styleUrls: ["./indicator-verdict-hero.component.scss"],
})
export class IndicatorVerdictHeroComponent {
  readonly analysis = input.required<VerdictAnalysis>();
  readonly cta = output<VerdictCta>();

  readonly verdict = computed<Verdict>(() => computeVerdict(this.analysis()));

  readonly headline = computed(() => verdictHeadline(this.verdict()));

  readonly tierColor = computed<string>(() => {
    const tier = this.verdict().tier;
    if (tier === "pre_flight") return "var(--bull)";
    if (tier === "investigate") return "var(--warn)";
    return "var(--bear)";
  });

  readonly tierLabel = computed<string>(() => {
    const tier = this.verdict().tier;
    if (tier === "pre_flight") return "PRE-FLIGHT";
    if (tier === "investigate") return "INVESTIGATE";
    return "REJECT";
  });

  // Gauge geometry for the 3/4-circle stroke.
  readonly gaugeRadius = 90;
  readonly gaugeCircumference = 2 * Math.PI * this.gaugeRadius;
  readonly gaugeVisible = this.gaugeCircumference * 0.75;
  readonly gaugeDash = computed(() => {
    const v = this.verdict();
    return (v.screensPassed / v.screensTotal) * this.gaugeVisible;
  });
  readonly gaugeOffset = computed(
    () => this.gaugeCircumference - this.gaugeVisible
  );

  /** Pre-formatted percent strings derived from the raw [0, 1] fractions
   *  on the analysis. Earlier we glued ``%`` onto the raw fraction
   *  which made 0.701 render as 0.7%. */
  readonly highVolHitRatePct = computed<string | null>(() => {
    const v = this.analysis().highVolHitRate;
    return v === null ? null : `${(v * 100).toFixed(1)}%`;
  });

  readonly lowVolHitRatePct = computed<string | null>(() => {
    const v = this.analysis().lowVolHitRate;
    return v === null ? null : `${(v * 100).toFixed(1)}%`;
  });

  /** OOS-vs-IS delta rounded to whole percent. The host page often
   *  passes in a fractional number (e.g. 14.978276111679524) which we
   *  must not display verbatim. */
  readonly oosVsIsPctRounded = computed<number | null>(() => {
    const v = this.analysis().oosVsIsPct;
    return v === null ? null : Math.round(v);
  });

  /** Pills summarising the 5 decision checks plus contextual demotions. */
  readonly pills = computed(() => {
    const a = this.analysis();
    const oosVs = this.oosVsIsPctRounded();
    const oosSuffix = oosVs !== null
      ? ` (${oosVs >= 0 ? "+" : ""}${oosVs}%)`
      : "";
    const magnitude = Math.abs(a.oosIc).toFixed(3);
    const pills: { label: string; kind: "good" | "bad" | "warn" }[] = [
      { label: `FDR ${a.fdrSignificant ? "✓" : "✗"}`, kind: a.fdrSignificant ? "good" : "bad" },
      { label: `Bonferroni ${a.bonferroniSignificant ? "✓" : "✗"}`, kind: a.bonferroniSignificant ? "good" : "bad" },
      { label: `OOS ${a.oosHolds ? "holds" : "drops"}${oosSuffix}`, kind: a.oosHolds ? "good" : "bad" },
      {
        label: `|IC| ${magnitude} ${a.largeStatisticalEffect ? ">" : "<"} 0.10 magnitude`,
        kind: a.largeStatisticalEffect ? "good" : "warn",
      },
    ];

    if (a.highVolBoostPct !== null && a.highVolBoostPct > 0) {
      pills.push({ label: `Stronger in high-vol`, kind: "good" });
    }
    if (a.singleAsset) {
      pills.push({ label: `Single asset only`, kind: "warn" });
    }
    return pills;
  });

  /** Blockers shown when the screens-passed count would otherwise
   *  qualify for pre-flight but a hard demotion fired. Empty when the
   *  tier matches the screen count. */
  readonly blockers = computed(() => this.verdict().blockers);

  /** Z-score is diagnostic-only when the random-shuffle baseline is
   *  small. The headline lede uses this to switch wording from
   *  "beats random by Xσ" to "outside the N-shuffle null". */
  readonly zScoreClaimIsLiteral = computed(() => this.analysis().randomShuffles >= 1000);

  readonly whenDetail = computed(() => {
    const a = this.analysis();
    return `IC peaks at ${a.bestHorizonLabel} and doesn't decay faster than that.`;
  });

  readonly whereAnswer = computed(() => {
    const a = this.analysis();
    if (a.highVolBoostPct === null) return "Regime data unavailable";
    return a.highVolBoostPct > 0 ? "High-vol regimes" : "Low-vol regimes";
  });

  readonly whereDetail = computed(() => {
    const a = this.analysis();
    const hv = this.highVolHitRatePct();
    const lv = this.lowVolHitRatePct();
    if (a.highVolBoostPct === null || hv === null || lv === null) {
      return "Vol-regime crosscheck not available for this run.";
    }
    const sign = a.highVolBoostPct >= 0 ? "+" : "";
    return `${sign}${a.highVolBoostPct}% stronger when rolling vol is above median. Hit-rate ${hv} vs ${lv}.`;
  });

  readonly howDetail = computed(() => {
    const a = this.analysis();
    const note = a.sharpe < 0
      ? " Negative — threshold-entry would be a losing trade before costs."
      : " Test with costs in Pre-flight.";
    return `Classic threshold; Sharpe proxy ${a.sharpe.toFixed(2)}.${note}`;
  });

  onCta(kind: VerdictCta): void {
    this.cta.emit(kind);
  }

  pillColorVar(kind: string): string {
    if (kind === "good") return "var(--bull)";
    if (kind === "warn") return "var(--warn)";
    if (kind === "bad") return "var(--bear)";
    return "var(--text-muted)";
  }

  tierCssClass(tier: VerdictTier): string {
    return `verdict--${tier}`;
  }
}

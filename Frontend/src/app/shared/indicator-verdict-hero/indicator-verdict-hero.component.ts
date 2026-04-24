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
 *   1. Confidence gauge (0-100 with TRADE / INVESTIGATE / REJECT tier)
 *   2. Headline verdict + reason pills
 *   3. Three big-action CTAs
 *
 * Below that: a 3-cell WHEN / WHERE / HOW decision band.
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
    if (tier === "trade") return "var(--bull)";
    if (tier === "investigate") return "var(--warn)";
    return "var(--bear)";
  });

  readonly tierLabel = computed<string>(() => {
    const tier = this.verdict().tier;
    if (tier === "trade") return "TRADE";
    if (tier === "investigate") return "INVESTIGATE";
    return "REJECT";
  });

  // Gauge geometry for the 3/4-circle stroke.
  readonly gaugeRadius = 90;
  readonly gaugeCircumference = 2 * Math.PI * this.gaugeRadius;
  readonly gaugeVisible = this.gaugeCircumference * 0.75;
  readonly gaugeDash = computed(
    () => (this.verdict().score / 100) * this.gaugeVisible
  );
  readonly gaugeOffset = computed(
    () => this.gaugeCircumference - this.gaugeVisible
  );

  /** Pills summarising the 5 decision checks plus a single caveat. */
  readonly pills = computed(() => {
    const a = this.analysis();
    const oosVs = a.oosVsIsPct;
    const oosSuffix = oosVs !== null
      ? ` (${oosVs >= 0 ? "+" : ""}${oosVs}%)`
      : "";
    const magnitude = Math.abs(a.oosIc).toFixed(3);
    return [
      { label: `FDR ${a.fdrSignificant ? "✓" : "✗"}`, kind: a.fdrSignificant ? "good" : "bad" },
      { label: `Bonferroni ${a.bonferroniSignificant ? "✓" : "✗"}`, kind: a.bonferroniSignificant ? "good" : "bad" },
      { label: `OOS ${a.oosHolds ? "holds" : "drops"}${oosSuffix}`, kind: a.oosHolds ? "good" : "bad" },
      { label: `|IC| ${magnitude} ${a.economicallyMeaningful ? ">" : "<"} 0.10`, kind: a.economicallyMeaningful ? "good" : "warn" },
      ...(a.highVolBoostPct !== null && a.highVolBoostPct > 0
        ? [{ label: `Stronger in high-vol`, kind: "good" as const }]
        : []),
      ...(a.singleAsset ? [{ label: `Single asset only`, kind: "warn" as const }] : []),
    ];
  });

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
    if (
      a.highVolBoostPct === null ||
      a.highVolHitRate === null ||
      a.lowVolHitRate === null
    ) {
      return "Vol-regime crosscheck not available for this run.";
    }
    const sign = a.highVolBoostPct >= 0 ? "+" : "";
    return `${sign}${a.highVolBoostPct}% stronger when rolling vol is above median. Hit-rate ${a.highVolHitRate}% vs ${a.lowVolHitRate}%.`;
  });

  readonly howDetail = computed(() => {
    const a = this.analysis();
    return `Classic threshold; Sharpe proxy ${a.sharpe.toFixed(2)}. Test with costs in Pre-flight.`;
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

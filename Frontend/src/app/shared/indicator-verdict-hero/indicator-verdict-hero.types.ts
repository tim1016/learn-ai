/**
 * Slimmed-down shape of the indicator-reliability analysis that the
 * verdict hero needs to render. The host page (which already produces a
 * much richer ``IndicatorReliabilityResponse``) builds this object and
 * feeds it in.
 *
 * Keeping this type private to the verdict-hero module so the shared
 * component never imports the full response type from the Research Lab
 * component — cleanly decoupled.
 */
export interface VerdictAnalysis {
  /** Indicator display name (e.g. "RSI(14)"). */
  indicatorDisplay: string;
  /** Underlying ticker (e.g. "AAPL"). */
  ticker: string;

  /** Best horizon label (e.g. "30-bar"). */
  bestHorizonLabel: string;
  /** Out-of-sample IC at the best horizon. */
  oosIc: number;
  /** In-sample IC at the best horizon. */
  isIc: number;
  /** Relative delta (OOS vs IS) as a whole-percent number (e.g. -12). */
  oosVsIsPct: number | null;
  /** Sharpe proxy at the best horizon (for the "How to enter" line). */
  sharpe: number;

  /** Pass flags for each of the 5 decision checks. */
  fdrSignificant: boolean;
  bonferroniSignificant: boolean;
  /** OOS retention is considered to "hold" if retention >= 60 %. */
  oosHolds: boolean;
  /** Z-score against random baseline (signed). */
  zScore: number;
  /** True if |IC| crosses the 0.10 economic-meaning threshold. */
  economicallyMeaningful: boolean;

  /** Vol-regime hit rates at the best horizon, 0..100. */
  highVolHitRate: number | null;
  lowVolHitRate: number | null;
  /** IC improvement percentage in high vs low vol (whole-percent). */
  highVolBoostPct: number | null;

  /** True when the result applies to a single asset (no cross-section). */
  singleAsset: boolean;

  /** Human-language direction descriptor (e.g. "mean-reversion"). */
  directionLabel: string;

  /** Number of random shuffles used for the baseline. */
  randomShuffles: number;
}

export type VerdictTier = "trade" | "investigate" | "reject";

export interface Verdict {
  score: number; // 0..100
  tier: VerdictTier;
}

export function computeVerdict(a: VerdictAnalysis): Verdict {
  const checks = [
    a.fdrSignificant ? 20 : 0,
    a.bonferroniSignificant ? 20 : 0,
    a.oosHolds ? 20 : 0,
    Math.abs(a.zScore) > 3 ? 20 : 0,
    a.economicallyMeaningful ? 20 : 10,
  ];
  const score = checks.reduce((s, v) => s + v, 0);
  const tier: VerdictTier =
    score >= 85 ? "trade" : score >= 60 ? "investigate" : "reject";
  return { score, tier };
}

export function verdictHeadline(v: Verdict): string {
  return v.tier === "trade"
    ? "Ready to trade"
    : v.tier === "investigate"
      ? "Worth a closer look"
      : "Not trade-ready";
}

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
  /** True if |IC| crosses the 0.10 magnitude threshold. Note: this is a
   *  *statistical* magnitude floor, not an *economic* viability claim —
   *  v2 review fix. Real economic viability requires turnover / cost /
   *  spread modelling, which the Indicator Reliability page does not
   *  do. The pill is labelled accordingly. */
  largeStatisticalEffect: boolean;

  /** Vol-regime hit rates at the best horizon, **as fractions in
   *  [0, 1]** (not pre-multiplied by 100). The hero formats these to
   *  display percent. v2 review fix — earlier the hero glued ``%``
   *  onto the raw fraction so 0.701 rendered as 0.7%. */
  highVolHitRate: number | null;
  lowVolHitRate: number | null;
  /** IC improvement percentage in high vs low vol (whole-percent). */
  highVolBoostPct: number | null;

  /** True when the result applies to a single asset (no cross-section).
   *  v2 review fix: this now hard-blocks the "pre-flight candidate"
   *  tier rather than merely showing a chip — single-asset evidence
   *  is not enough to claim the *indicator* itself is reliable. */
  singleAsset: boolean;

  /** Human-language direction descriptor (e.g. "mean-reversion"). */
  directionLabel: string;

  /** Number of random shuffles used for the baseline. The hero treats
   *  any z-score claim built on < 1000 shuffles as "outside null;
   *  diagnostic only" rather than a literal sigma claim. */
  randomShuffles: number;
}

/**
 * Tiers (renamed in v2 review).
 *
 * - ``pre_flight`` (was ``trade``): all 5 statistical screens pass and
 *   no hard demotions fire. Eligible for the Strategy Lab pre-flight
 *   pipeline; **not** eligible for live trading on this signal alone.
 * - ``investigate``: at least 3 of 5 screens pass, OR all 5 pass but a
 *   hard demotion fired (single-asset only / negative Sharpe proxy /
 *   undersized random shuffle).
 * - ``reject``: fewer than 3 of 5 screens pass.
 */
export type VerdictTier = "pre_flight" | "investigate" | "reject";

export interface ScreenResult {
  name: string;
  passed: boolean;
  reason?: string;
}

export interface Verdict {
  /** How many of the 5 statistical screens passed (0..5). The UI
   *  surfaces this as ``Screens N/5`` rather than as a 0-100 scalar
   *  "confidence" score — v2 review correctly identified the latter as
   *  reintroducing a single number the framework explicitly rejected. */
  screensPassed: number;
  screensTotal: 5;
  tier: VerdictTier;
  /** Reasons the verdict was demoted from pre-flight despite passing
   *  enough screens. Empty when the tier matches the screen count. */
  blockers: string[];
}

export function computeVerdict(a: VerdictAnalysis): Verdict {
  const screens = [
    a.fdrSignificant,
    a.bonferroniSignificant,
    a.oosHolds,
    Math.abs(a.zScore) > 3,
    a.largeStatisticalEffect,
  ];
  const screensPassed = screens.filter(Boolean).length;

  // Hard demotions — these do not subtract from screensPassed (those
  // count statistical evidence) but they prevent the verdict from
  // reaching pre-flight tier. Surfaced as blockers so the reader can
  // see *why* a 5/5 result is still not pre-flight-ready.
  const blockers: string[] = [];
  if (a.singleAsset) {
    blockers.push(
      "Single-asset evidence — indicator-level reliability requires cross-ticker replication."
    );
  }
  if (a.sharpe < 0) {
    blockers.push(
      `Sharpe proxy ${a.sharpe.toFixed(2)} is negative — threshold-entry P&L would lose money before costs.`
    );
  }
  if (a.randomShuffles < 1000) {
    blockers.push(
      `Random baseline used ${a.randomShuffles} shuffles; tail z-score is diagnostic only (< 1000 shuffles).`
    );
  }

  let tier: VerdictTier;
  if (screensPassed === 5 && blockers.length === 0) {
    tier = "pre_flight";
  } else if (screensPassed >= 3) {
    tier = "investigate";
  } else {
    tier = "reject";
  }

  return { screensPassed, screensTotal: 5, tier, blockers };
}

export function verdictHeadline(v: Verdict): string {
  if (v.tier === "pre_flight") return "Pre-flight candidate";
  if (v.tier === "investigate") return "Worth investigating";
  return "Not validated";
}

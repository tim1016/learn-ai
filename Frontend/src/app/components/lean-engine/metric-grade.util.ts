/**
 * Shared metric-grading logic for hero cards, benchmark scorecard, and
 * tooltips. Thresholds align with the Engine Lab Forensic Analysis doc
 * (2025–26 institutional hurdles).
 */

export type Band = "green" | "amber" | "red" | "skeptical" | "na";

export interface HeroGrade {
  band: Band;
  subtitle: string;
  targetRange: string;
  /** Anchor id in the Engine Docs tab; used to deep-link from tooltips. */
  docAnchor: string;
}

const DOC_ROUTE = "/engine-docs";

export function gradeSharpe(v: number | null | undefined): HeroGrade {
  const a = anchor("sharpe");
  if (!isNum(v)) return { band: "na", subtitle: "Not computed this window.", targetRange: "1.0 – 2.0", docAnchor: a };
  if (v < 0.5) return { band: "red", subtitle: "Below professional viability.", targetRange: "1.0 – 2.0", docAnchor: a };
  if (v < 1.0) return { band: "amber", subtitle: "Below the institutional floor.", targetRange: "1.0 – 2.0", docAnchor: a };
  if (v < 2.0) return { band: "green", subtitle: "Clears the institutional floor.", targetRange: "1.0 – 2.0", docAnchor: a };
  if (v < 3.0) return { band: "green", subtitle: "Elite — verify out-of-sample.", targetRange: "1.0 – 2.0", docAnchor: a };
  return { band: "skeptical", subtitle: "Suspiciously high (>3.0) — likely overfit.", targetRange: "1.0 – 2.0", docAnchor: a };
}

export function gradeSortino(v: number | null | undefined): HeroGrade {
  const a = anchor("sortino");
  if (!isNum(v)) return { band: "na", subtitle: "No negative-return bars this window.", targetRange: "≥ 1.5", docAnchor: a };
  if (v < 1.0) return { band: "red", subtitle: "Downside risk dominates.", targetRange: "≥ 1.5", docAnchor: a };
  if (v < 1.5) return { band: "amber", subtitle: "Short of the 1.5 baseline.", targetRange: "≥ 1.5", docAnchor: a };
  if (v < 3.0) return { band: "green", subtitle: "Meets institutional baseline.", targetRange: "≥ 1.5", docAnchor: a };
  return { band: "skeptical", subtitle: "Extreme Sortino — validate sample.", targetRange: "≥ 1.5", docAnchor: a };
}

export function gradeProfitFactor(v: number | null | undefined): HeroGrade {
  const a = anchor("profit-factor");
  if (!isNum(v)) return { band: "na", subtitle: "Unavailable.", targetRange: "1.75 – 3.0", docAnchor: a };
  if (!isFinite(v)) return { band: "amber", subtitle: "Zero losing trades — need a longer window.", targetRange: "1.75 – 3.0", docAnchor: a };
  if (v < 1.0) return { band: "red", subtitle: "Losing system.", targetRange: "1.75 – 3.0", docAnchor: a };
  if (v < 1.75) return { band: "amber", subtitle: "Marginal — below 1.75 threshold.", targetRange: "1.75 – 3.0", docAnchor: a };
  if (v <= 3.0) return { band: "green", subtitle: "Healthy win/loss ratio.", targetRange: "1.75 – 3.0", docAnchor: a };
  if (v <= 4.0) return { band: "green", subtitle: "Elite efficiency — stress-test.", targetRange: "1.75 – 3.0", docAnchor: a };
  return { band: "skeptical", subtitle: "PF > 4 rarely survives out-of-sample.", targetRange: "1.75 – 3.0", docAnchor: a };
}

export function gradeWinRate(v: number | null | undefined): HeroGrade {
  const a = anchor("win-rate");
  if (!isNum(v)) return { band: "na", subtitle: "Unavailable.", targetRange: "55% – 75% (mean-rev)", docAnchor: a };
  if (v < 0.4) return { band: "red", subtitle: "Too low unless payoff is very high.", targetRange: "55% – 75% (mean-rev)", docAnchor: a };
  if (v < 0.55) return { band: "amber", subtitle: "Below typical mean-reversion range.", targetRange: "55% – 75% (mean-rev)", docAnchor: a };
  if (v <= 0.85) return { band: "green", subtitle: "In-range for a mean-reverting edge.", targetRange: "55% – 75% (mean-rev)", docAnchor: a };
  return { band: "skeptical", subtitle: "Above 85% — data-leak red flag.", targetRange: "55% – 75% (mean-rev)", docAnchor: a };
}

export function gradeMaxDrawdown(v: number | null | undefined): HeroGrade {
  const a = anchor("max-drawdown");
  if (!isNum(v)) return { band: "na", subtitle: "Unavailable.", targetRange: "< 15%", docAnchor: a };
  if (v < 0.05) return { band: "green", subtitle: "Superior capital preservation.", targetRange: "< 15%", docAnchor: a };
  if (v < 0.15) return { band: "green", subtitle: "Within institutional tolerance.", targetRange: "< 15%", docAnchor: a };
  if (v < 0.2) return { band: "amber", subtitle: "Approaching the 20% cap.", targetRange: "< 15%", docAnchor: a };
  return { band: "red", subtitle: "Exceeds institutional limits.", targetRange: "< 15%", docAnchor: a };
}

export function gradeExpectancy(v: number | null | undefined): HeroGrade {
  const a = anchor("expectancy");
  if (!isNum(v)) return { band: "na", subtitle: "Unavailable.", targetRange: "> 0 after fees", docAnchor: a };
  if (v <= 0) return { band: "red", subtitle: "Loses money per trade on average.", targetRange: "> 0 after fees", docAnchor: a };
  if (v < 0.001) return { band: "amber", subtitle: "Thin edge — fees may erase it.", targetRange: "> 0 after fees", docAnchor: a };
  return { band: "green", subtitle: "Healthy per-trade edge.", targetRange: "> 0 after fees", docAnchor: a };
}

export function gradeNetProfit(v: number | null | undefined, initial: number | null | undefined): HeroGrade {
  const a = anchor("net-profit");
  if (!isNum(v) || !isNum(initial) || initial === 0) {
    return { band: "na", subtitle: "Unavailable.", targetRange: "Positive after fees", docAnchor: a };
  }
  const pct = v / initial;
  if (v <= 0) return { band: "red", subtitle: "Strategy lost money.", targetRange: "Positive after fees", docAnchor: a };
  if (pct < 0.02) return { band: "amber", subtitle: "Marginal return — beaten by T-bills.", targetRange: "Positive after fees", docAnchor: a };
  if (pct < 0.1) return { band: "green", subtitle: "Modest positive return.", targetRange: "Positive after fees", docAnchor: a };
  return { band: "green", subtitle: "Strong absolute return.", targetRange: "Positive after fees", docAnchor: a };
}

// ----- Helpers --------------------------------------------------------------

function isNum(v: number | null | undefined): v is number {
  return typeof v === "number" && !Number.isNaN(v);
}

function anchor(id: string): string {
  return `${DOC_ROUTE}#${id}`;
}

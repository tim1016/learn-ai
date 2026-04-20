import { ChangeDetectionStrategy, Component, computed, input } from "@angular/core";
import { CommonModule } from "@angular/common";

export type Grade = "green" | "amber" | "red" | "skeptical" | "na";

export interface ScorecardResultLike {
  statistics: Record<string, number | null> | null | undefined;
  win_rate: number | null | undefined;
}

interface ScoredMetric {
  key: string;
  label: string;
  target: string;
  value: number | null;
  display: string;
  grade: Grade;
  verdict: string;
  note?: string;
}

@Component({
  selector: "app-benchmark-scorecard",
  standalone: true,
  imports: [CommonModule],
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: "./benchmark-scorecard.component.html",
  styleUrls: ["./benchmark-scorecard.component.scss"],
})
export class BenchmarkScorecardComponent {
  readonly result = input<ScorecardResultLike | null>(null);

  readonly hasResult = computed<boolean>(() => {
    const r = this.result();
    return !!r && !!r.statistics;
  });

  readonly scored = computed<ScoredMetric[]>(() => {
    const r = this.result();
    if (!r || !r.statistics) return [];
    const s = r.statistics;
    const winRate = typeof r.win_rate === "number" ? r.win_rate : null;

    return [
      this.gradeSharpe(num(s["sharpe_ratio"])),
      this.gradeSortino(num(s["sortino_ratio"])),
      this.gradeProfitFactor(num(s["profit_factor"])),
      this.gradeWinRate(winRate),
      this.gradeDrawdown(num(s["max_drawdown_pct"])),
      this.gradeExpectancy(num(s["expectancy_pct"])),
    ];
  });

  readonly overallGrade = computed<Grade>(() => {
    const all = this.scored();
    if (all.length === 0) return "na";
    if (all.some((m) => m.grade === "red")) return "red";
    if (all.some((m) => m.grade === "skeptical")) return "skeptical";
    if (all.some((m) => m.grade === "amber")) return "amber";
    if (all.every((m) => m.grade === "green" || m.grade === "na")) return "green";
    return "amber";
  });

  readonly overallSummary = computed<string>(() => {
    switch (this.overallGrade()) {
      case "green":
        return "All measured metrics clear institutional hurdles.";
      case "amber":
        return "Mostly healthy — one or more metrics sit in the caution band.";
      case "red":
        return "At least one metric fails the institutional threshold. Review the red rows before deploying.";
      case "skeptical":
        return "Performance is suspiciously strong. Stress for look-ahead bias and overfitting before trusting the equity curve.";
      default:
        return "Run a backtest to populate the scorecard.";
    }
  });

  // ------------------------------------------------------------------
  // Individual graders — thresholds from the Forensic Analysis doc
  // ------------------------------------------------------------------
  private gradeSharpe(v: number | null): ScoredMetric {
    const base: ScoredMetric = {
      key: "sharpe",
      label: "Sharpe Ratio",
      target: "1.0 – 2.0 (institutional)",
      value: v,
      display: v === null ? "—" : v.toFixed(4),
      grade: "na",
      verdict: "",
    };
    if (v === null) return { ...base, verdict: "Not computed for this run." };
    if (v < 0.5) return { ...base, grade: "red", verdict: "Below professional viability (<0.5)." };
    if (v < 1.0) return { ...base, grade: "amber", verdict: "Below the 1.0 institutional floor." };
    if (v < 2.0) return { ...base, grade: "green", verdict: "Clears the institutional floor cleanly." };
    if (v < 3.0)
      return {
        ...base,
        grade: "green",
        verdict: "Elite tier — near the skepticism threshold.",
        note: "Stress-test for look-ahead bias when Sharpe > 2.0.",
      };
    return {
      ...base,
      grade: "skeptical",
      verdict: "Suspiciously high (>3.0). Likely overfit or data leakage.",
      note: "Re-run on out-of-sample data and verify no future info leaks.",
    };
  }

  private gradeSortino(v: number | null): ScoredMetric {
    const base: ScoredMetric = {
      key: "sortino",
      label: "Sortino Ratio",
      target: "≥ 1.5",
      value: v,
      display: v === null ? "—" : v.toFixed(4),
      grade: "na",
      verdict: "",
    };
    if (v === null) return { ...base, verdict: "No negative-return bars this window." };
    if (v < 1.0) return { ...base, grade: "red", verdict: "Downside risk dominates — below 1.0." };
    if (v < 1.5) return { ...base, grade: "amber", verdict: "Acceptable but short of the 1.5 baseline." };
    if (v < 3.0) return { ...base, grade: "green", verdict: "Meets or exceeds the institutional baseline." };
    return { ...base, grade: "skeptical", verdict: "Extreme Sortino (>3.0) — validate sample size." };
  }

  private gradeProfitFactor(v: number | null): ScoredMetric {
    const base: ScoredMetric = {
      key: "profit_factor",
      label: "Profit Factor",
      target: "1.75 – 3.0",
      value: v,
      display: v === null ? "—" : isFinite(v) ? v.toFixed(4) : "∞",
      grade: "na",
      verdict: "",
    };
    if (v === null) return { ...base, verdict: "Unavailable." };
    if (!isFinite(v))
      return {
        ...base,
        grade: "amber",
        verdict: "Zero losing trades this window — PF is ∞.",
        note: "Too few losses yet to grade. Extend the window.",
      };
    if (v < 1.0) return { ...base, grade: "red", verdict: "Losing system — gross losses exceed gross wins." };
    if (v < 1.75) return { ...base, grade: "amber", verdict: "Marginal — below the 1.75 institutional threshold." };
    if (v <= 3.0) return { ...base, grade: "green", verdict: "Healthy win/loss ratio." };
    if (v <= 4.0) return { ...base, grade: "green", verdict: "Elite-tier efficiency.", note: "Stress-test for overfitting." };
    return { ...base, grade: "skeptical", verdict: "PF > 4 is rarely real out-of-sample." };
  }

  private gradeWinRate(v: number | null): ScoredMetric {
    const base: ScoredMetric = {
      key: "win_rate",
      label: "Win Rate",
      target: "55% – 75% (mean-reversion)",
      value: v,
      display: v === null ? "—" : `${(v * 100).toFixed(2)}%`,
      grade: "na",
      verdict: "",
    };
    if (v === null) return { ...base, verdict: "Unavailable." };
    if (v < 0.4) return { ...base, grade: "red", verdict: "Too low unless the payoff ratio is very high." };
    if (v < 0.55) return { ...base, grade: "amber", verdict: "Below typical mean-reversion range." };
    if (v <= 0.85) return { ...base, grade: "green", verdict: "In-range for a mean-reverting edge." };
    return {
      ...base,
      grade: "skeptical",
      verdict: "Above 85% is a red flag — check for look-ahead or survivorship bias.",
    };
  }

  private gradeDrawdown(v: number | null): ScoredMetric {
    const base: ScoredMetric = {
      key: "max_drawdown",
      label: "Max Drawdown",
      target: "< 15%",
      value: v,
      display: v === null ? "—" : `${(v * 100).toFixed(2)}%`,
      grade: "na",
      verdict: "",
    };
    if (v === null) return { ...base, verdict: "Unavailable." };
    if (v < 0.05) return { ...base, grade: "green", verdict: "Superior capital preservation.", note: "Verify window is long enough to be meaningful." };
    if (v < 0.15) return { ...base, grade: "green", verdict: "Within the institutional tolerance band." };
    if (v < 0.2) return { ...base, grade: "amber", verdict: "Approaching the 20% institutional cap." };
    return { ...base, grade: "red", verdict: "Exceeds typical institutional drawdown limits." };
  }

  private gradeExpectancy(v: number | null): ScoredMetric {
    const base: ScoredMetric = {
      key: "expectancy",
      label: "Expectancy (per trade)",
      target: "> 0 after fees",
      value: v,
      display: v === null ? "—" : `${(v * 100).toFixed(4)}%`,
      grade: "na",
      verdict: "",
    };
    if (v === null) return { ...base, verdict: "Unavailable." };
    if (v <= 0) return { ...base, grade: "red", verdict: "Non-positive edge — strategy loses money per trade on average." };
    if (v < 0.001) return { ...base, grade: "amber", verdict: "Edge is thin — fees and slippage may erase it live." };
    return { ...base, grade: "green", verdict: "Healthy per-trade edge." };
  }
}

function num(v: number | null | undefined): number | null {
  if (v === null || v === undefined) return null;
  if (typeof v !== "number" || Number.isNaN(v)) return null;
  return v;
}

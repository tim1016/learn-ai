/**
 * Production Readiness scoring for Engine Lab backtest results.
 *
 * Adapted from the trading-skills composite-score pattern: five weighted
 * dimensions, each 0–100, built from up to five sub-scores at 0–20. Sub-scores
 * that can't be computed (missing backend fields) are marked unavailable and
 * drop out of their dimension's average; fully-unavailable dimensions drop
 * out of the composite with proportional re-weighting.
 *
 * Thresholds follow the Engine Lab Forensic Analysis doc: institutional
 * hurdles for 2025–26 systematic equity strategies, with explicit skepticism
 * penalties for Sharpe > 3, PF > 4, and Win Rate > 85%.
 */

export interface ReadinessResultLike {
  statistics: Record<string, number | null> | null | undefined;
  win_rate: number | null | undefined;
  total_trades: number | null | undefined;
  net_profit: number | null | undefined;
  total_fees: number | null | undefined;
  lean_statistics: LeanStatsLike | null | undefined;
}

export interface LeanStatsLike {
  portfolio?: Partial<LeanPortfolioLike>;
  trade?: Partial<LeanTradeLike>;
}

interface LeanPortfolioLike {
  probabilistic_sharpe_ratio: number;
  compounding_annual_return: number;
  drawdown: number;
  drawdown_recovery: number;
  sharpe_ratio: number;
  sortino_ratio: number;
  annual_standard_deviation: number;
}

interface LeanTradeLike {
  profit_factor: number;
  sharpe_ratio: number;
  max_consecutive_losing_trades: number;
  average_profit: number;
  average_loss: number;
}

export type Grade = "A+" | "A" | "B" | "C" | "D" | "F";
export type Signal = "Deploy" | "Paper-trade" | "Iterate" | "Rework" | "Reject";
export type ReadinessEngine = "python" | "lean";

export interface SubScore {
  key: string;
  label: string;
  score: number | null;
  rawValue: number | null;
  display: string;
  note: string;
}

export interface DimensionScore {
  key: string;
  label: string;
  weight: number;
  score: number | null;
  subScores: SubScore[];
  summary: string;
}

export interface ReadinessReport {
  composite: number | null;
  grade: Grade | null;
  signal: Signal | null;
  verdict: string;
  dimensions: DimensionScore[];
  missingMetrics: string[];
  normalizedWeights: boolean;
}

// ----- Public entry point ---------------------------------------------------

/**
 * Legacy UI mirror of the backend-authored RunVerdict v1 scorer.
 *
 * Formula: Weighted production-readiness composite from five 0-100 dimensions
 *   with unavailable sub-scores omitted and scored dimensions reweighted.
 * Reference: Frontend scorer at commit fe0e9e1c1.
 * Canonical implementation: PythonDataService/app/services/run_verdict_service.py.
 * Validated against: Frontend/src/app/components/lean-engine/readiness-score-card/
 *   readiness-score.util.spec.ts and PythonDataService/tests/services/test_run_verdict_parity.py.
 */
export function computeReadiness(result: ReadinessResultLike | null, engine: ReadinessEngine = "python"): ReadinessReport {
  if (!result || !result.statistics) return emptyReport("Run a backtest to generate a Production Readiness score.");

  const dimensions: DimensionScore[] = [
    scoreReturnQuality(result),
    scoreRiskControl(result),
    scoreTradeEdge(result),
    scoreStatisticalConfidence(result, engine),
    scoreAlphaCalibration(result),
  ];

  const missingMetrics = dimensions.flatMap((d) =>
    d.subScores.filter((s) => s.score === null).map((s) => `${d.label}: ${s.label}`),
  );

  const scored = dimensions.filter((d) => d.score !== null);
  const totalWeight = scored.reduce((acc, d) => acc + d.weight, 0);
  const normalizedWeights = totalWeight > 0 && Math.abs(totalWeight - 1) > 1e-6;

  if (scored.length === 0 || totalWeight === 0) {
    return { ...emptyReport("Not enough data to grade."), dimensions, missingMetrics };
  }

  const composite = Math.round(
    scored.reduce((acc, d) => acc + (d.score ?? 0) * (d.weight / totalWeight), 0),
  );
  const { grade, signal, verdict } = gradeAndSignal(composite, missingMetrics.length);

  return {
    composite,
    grade,
    signal,
    verdict,
    dimensions,
    missingMetrics,
    normalizedWeights,
  };
}

function emptyReport(verdict: string): ReadinessReport {
  return { composite: null, grade: null, signal: null, verdict, dimensions: [], missingMetrics: [], normalizedWeights: false };
}

// ----- Grade + signal ladder ------------------------------------------------

function gradeAndSignal(score: number, missingCount: number): { grade: Grade; signal: Signal; verdict: string } {
  let grade: Grade;
  let signal: Signal;
  let verdict: string;
  if (score >= 85) { grade = "A+"; signal = "Deploy"; verdict = "Institutional-grade. Ready for live deployment at standard size."; }
  else if (score >= 70) { grade = "A"; signal = "Paper-trade"; verdict = "Strong backtest. Paper-trade for 30 days before sizing up."; }
  else if (score >= 55) { grade = "B"; signal = "Iterate"; verdict = "Promising edge, but specific weaknesses need addressing before deployment."; }
  else if (score >= 40) { grade = "C"; signal = "Rework"; verdict = "Material problems — core parameters or logic need revisiting."; }
  else if (score >= 25) { grade = "D"; signal = "Rework"; verdict = "Fundamental issues. Rework the thesis, not just the parameters."; }
  else { grade = "F"; signal = "Reject"; verdict = "Reject — the backtest does not clear baseline viability."; }

  if (missingCount > 5) verdict += ` ${missingCount} sub-scores unavailable; grade may move once missing metrics are computed.`;
  return { grade, signal, verdict };
}

// ----- Dimensions -----------------------------------------------------------

function scoreReturnQuality(r: ReadinessResultLike): DimensionScore {
  const stats = r.statistics ?? {};
  const lean = r.lean_statistics?.portfolio ?? {};
  const subScores: SubScore[] = [
    gradeSharpeSub(num(stats["sharpe_ratio"]) ?? num(lean.sharpe_ratio)),
    gradeSortinoSub(num(stats["sortino_ratio"]) ?? num(lean.sortino_ratio)),
    gradeCagrSub(num(lean.compounding_annual_return)),
    gradeCalmarSub(num(stats["max_drawdown_pct"]) ?? num(lean.drawdown), num(lean.compounding_annual_return)),
    gradeAnnualVolSub(num(lean.annual_standard_deviation)),
  ];
  return {
    key: "return_quality",
    label: "Return Quality",
    weight: 0.25,
    score: averageSubs(subScores),
    subScores,
    summary: "Does the strategy make money efficiently per unit of risk?",
  };
}

function scoreRiskControl(r: ReadinessResultLike): DimensionScore {
  const stats = r.statistics ?? {};
  const lean = r.lean_statistics?.portfolio ?? {};
  const tr = r.lean_statistics?.trade ?? {};
  const subScores: SubScore[] = [
    gradeMaxDrawdownSub(num(stats["max_drawdown_pct"]) ?? num(lean.drawdown)),
    gradeRecoverySub(num(lean.drawdown_recovery)),
    gradeConsecutiveLossesSub(num(tr.max_consecutive_losing_trades)),
    { key: "dd_duration", label: "Drawdown duration", score: null, rawValue: null, display: "—", note: "Not yet computed — needs equity-curve timestamps." },
    { key: "downside_vol", label: "Downside volatility", score: null, rawValue: null, display: "—", note: "Planned — uses Sortino's σ_d separately." },
  ];
  return {
    key: "risk_control",
    label: "Risk Control",
    weight: 0.20,
    score: averageSubs(subScores),
    subScores,
    summary: "Does the strategy preserve capital when it's wrong?",
  };
}

function scoreTradeEdge(r: ReadinessResultLike): DimensionScore {
  const stats = r.statistics ?? {};
  const tr = r.lean_statistics?.trade ?? {};
  const payoff = payoffRatio(num(tr.average_profit), num(tr.average_loss));
  const subScores: SubScore[] = [
    gradeProfitFactorSub(num(stats["profit_factor"]) ?? num(tr.profit_factor)),
    gradeExpectancySub(num(stats["expectancy_pct"])),
    gradeWinRateSub(num(r.win_rate)),
    gradePayoffSub(payoff),
    gradeFeeDragSub(num(r.net_profit), num(r.total_fees)),
  ];
  return {
    key: "trade_edge",
    label: "Trade Edge",
    weight: 0.20,
    score: averageSubs(subScores),
    subScores,
    summary: "Is there a real per-trade edge after costs?",
  };
}

function scoreStatisticalConfidence(r: ReadinessResultLike, engine: ReadinessEngine): DimensionScore {
  const stats = r.statistics ?? {};
  const lean = r.lean_statistics?.portfolio ?? {};
  const tr = r.lean_statistics?.trade ?? {};
  const portfolioSharpe = num(stats["sharpe_ratio"]) ?? num(lean.sharpe_ratio);
  let tradeSharpe = num(tr.sharpe_ratio);
  if (engine === "lean" && tradeSharpe === 0) tradeSharpe = null;
  const subScores: SubScore[] = [
    gradePsrSub(num(lean.probabilistic_sharpe_ratio)),
    gradeSampleSizeSub(num(r.total_trades)),
    gradeSkepticismSub(portfolioSharpe, num(stats["profit_factor"]) ?? num(tr.profit_factor), num(r.win_rate)),
    gradeTradeGapSub(portfolioSharpe, tradeSharpe),
    { key: "benchmark", label: "Benchmark outperformance", score: null, rawValue: null, display: "—", note: "Planned — requires a Buy-and-Hold return series alongside the backtest." },
  ];
  return {
    key: "stat_confidence",
    label: "Statistical Confidence",
    weight: 0.20,
    score: averageSubs(subScores),
    subScores,
    summary: "Is the edge trustworthy, or sample-size / overfitting noise?",
  };
}

function scoreAlphaCalibration(_r: ReadinessResultLike): DimensionScore {
  const subScores: SubScore[] = [
    { key: "ece", label: "Expected Calibration Error", score: null, rawValue: null, display: "—", note: "Planned — derive from insight_summary confidence buckets." },
    { key: "conf_spread", label: "Over/under-confidence spread", score: null, rawValue: null, display: "—", note: "Planned — per-bucket accuracy minus emitted confidence." },
    { key: "magnitude_bias", label: "Magnitude bias", score: null, rawValue: null, display: "—", note: "Planned — mean of (actual − predicted) move." },
    { key: "worst_hour", label: "Worst-hour accuracy", score: null, rawValue: null, display: "—", note: "Planned — min accuracy across hour-of-day buckets." },
    { key: "regime_consistency", label: "Regime consistency", score: null, rawValue: null, display: "—", note: "Planned — rolling accuracy variance across market regimes." },
  ];
  return {
    key: "alpha_calibration",
    label: "Alpha Calibration",
    weight: 0.15,
    score: null,
    subScores,
    summary: "Does the alpha model's confidence match its empirical accuracy?",
  };
}

// ----- Sub-score graders ----------------------------------------------------

function gradeSharpeSub(v: number | null): SubScore {
  const base: SubScore = { key: "sharpe", label: "Sharpe ratio", score: null, rawValue: v, display: v === null ? "—" : v.toFixed(2), note: "" };
  if (v === null) return { ...base, note: "Not computed this window." };
  if (v < 0) return { ...base, score: 0, note: "Negative Sharpe — losing money on risk-adjusted basis." };
  if (v < 0.5) return { ...base, score: 4, note: "Below professional viability." };
  if (v < 1.0) return { ...base, score: 10, note: "Below the 1.0 institutional floor." };
  if (v < 1.5) return { ...base, score: 15, note: "Clears the institutional floor." };
  if (v < 2.0) return { ...base, score: 18, note: "Solidly institutional." };
  if (v < 3.0) return { ...base, score: 20, note: "Elite — verify out-of-sample." };
  return { ...base, score: 12, note: "Suspiciously high (>3.0) — likely overfit." };
}

function gradeSortinoSub(v: number | null): SubScore {
  const base: SubScore = { key: "sortino", label: "Sortino ratio", score: null, rawValue: v, display: v === null ? "—" : v.toFixed(2), note: "" };
  if (v === null) return { ...base, note: "No negative returns this window." };
  if (v < 0.5) return { ...base, score: 3, note: "Downside risk dominates." };
  if (v < 1.0) return { ...base, score: 8, note: "Below the 1.0 baseline." };
  if (v < 1.5) return { ...base, score: 13, note: "Approaching the 1.5 baseline." };
  if (v < 2.5) return { ...base, score: 18, note: "Meets the institutional baseline." };
  if (v < 4.0) return { ...base, score: 20, note: "Excellent downside profile." };
  return { ...base, score: 14, note: "Extreme Sortino — validate sample size." };
}

function gradeCagrSub(v: number | null): SubScore {
  const base: SubScore = { key: "cagr", label: "CAGR", score: null, rawValue: v, display: v === null ? "—" : `${(v * 100).toFixed(2)}%`, note: "" };
  if (v === null) return { ...base, note: "Not provided by engine (lean_statistics missing)." };
  if (v <= 0) return { ...base, score: 0, note: "Negative compound annual return." };
  if (v < 0.04) return { ...base, score: 6, note: "Below risk-free — consider T-bills." };
  if (v < 0.08) return { ...base, score: 11, note: "Below long-run equity baseline." };
  if (v < 0.15) return { ...base, score: 16, note: "Healthy annualized return." };
  if (v < 0.30) return { ...base, score: 20, note: "Elite annualized return." };
  return { ...base, score: 14, note: "Very high CAGR — check for overfitting or leverage." };
}

function gradeCalmarSub(maxDd: number | null, cagr: number | null): SubScore {
  const base: SubScore = { key: "calmar", label: "Calmar ratio", score: null, rawValue: null, display: "—", note: "" };
  if (cagr === null || maxDd === null || maxDd <= 0) {
    return { ...base, note: "Needs CAGR and Max DD to compute Calmar." };
  }
  const calmar = cagr / maxDd;
  base.rawValue = calmar;
  base.display = calmar.toFixed(2);
  if (calmar < 0) return { ...base, score: 0, note: "Negative Calmar." };
  if (calmar < 0.5) return { ...base, score: 5, note: "Return-to-risk ratio is weak." };
  if (calmar < 1.0) return { ...base, score: 10, note: "Below the 1.0 threshold." };
  if (calmar < 3.0) return { ...base, score: 15, note: "Healthy return-to-drawdown ratio." };
  if (calmar < 5.0) return { ...base, score: 20, note: "Elite Calmar." };
  return { ...base, score: 14, note: "Very high Calmar — verify the drawdown window is representative." };
}

function gradeAnnualVolSub(v: number | null): SubScore {
  const base: SubScore = { key: "annual_vol", label: "Annual volatility", score: null, rawValue: v, display: v === null ? "—" : `${(v * 100).toFixed(2)}%`, note: "" };
  if (v === null) return { ...base, note: "Not provided by engine." };
  if (v < 0.03) return { ...base, score: 20, note: "Very low volatility — stable return profile." };
  if (v < 0.10) return { ...base, score: 17, note: "Low volatility — below typical equity." };
  if (v < 0.20) return { ...base, score: 13, note: "Typical equity volatility." };
  if (v < 0.35) return { ...base, score: 8, note: "Elevated volatility." };
  return { ...base, score: 3, note: "Very high volatility — position sizing is critical." };
}

function gradeMaxDrawdownSub(v: number | null): SubScore {
  const base: SubScore = { key: "max_dd", label: "Max drawdown", score: null, rawValue: v, display: v === null ? "—" : `${(v * 100).toFixed(2)}%`, note: "" };
  if (v === null) return { ...base, note: "Not computed." };
  if (v < 0.02) return { ...base, score: 17, note: "Extreme preservation — verify window is long enough." };
  if (v < 0.05) return { ...base, score: 20, note: "Superior capital preservation." };
  if (v < 0.10) return { ...base, score: 18, note: "Excellent drawdown profile." };
  if (v < 0.15) return { ...base, score: 14, note: "Within institutional tolerance." };
  if (v < 0.20) return { ...base, score: 8, note: "Approaching the 20% institutional cap." };
  if (v < 0.30) return { ...base, score: 4, note: "Above typical institutional limit." };
  return { ...base, score: 0, note: "Fails typical risk-committee review." };
}

function gradeRecoverySub(v: number | null): SubScore {
  const base: SubScore = { key: "recovery", label: "Drawdown recovery", score: null, rawValue: v, display: v === null ? "—" : `${v} days`, note: "" };
  if (v === null) return { ...base, note: "Not provided by engine." };
  if (v <= 10) return { ...base, score: 20, note: "Quick recovery — strategy bounces back fast." };
  if (v <= 30) return { ...base, score: 16, note: "Healthy recovery window." };
  if (v <= 60) return { ...base, score: 12, note: "Moderate recovery time." };
  if (v <= 120) return { ...base, score: 8, note: "Long recovery — \"staircase\" pattern risk." };
  if (v <= 252) return { ...base, score: 4, note: "Nearly a full year to recover — investor patience risk." };
  return { ...base, score: 1, note: "Very long recovery — likely unacceptable for investors." };
}

function gradeConsecutiveLossesSub(v: number | null): SubScore {
  const base: SubScore = { key: "cons_losses", label: "Max consecutive losers", score: null, rawValue: v, display: v === null ? "—" : `${v}`, note: "" };
  if (v === null) return { ...base, note: "Not computed." };
  if (v <= 3) return { ...base, score: 20, note: "Resilient through streaks." };
  if (v <= 5) return { ...base, score: 16, note: "Typical losing streak length." };
  if (v <= 8) return { ...base, score: 10, note: "Long streak — psychologically hard to trade live." };
  if (v <= 12) return { ...base, score: 5, note: "Very long streak — kill-switch risk." };
  return { ...base, score: 0, note: "Extreme streak — most traders would bail before recovery." };
}

function gradeProfitFactorSub(v: number | null): SubScore {
  const base: SubScore = { key: "pf", label: "Profit factor", score: null, rawValue: v, display: v === null ? "—" : isFinite(v) ? v.toFixed(2) : "∞", note: "" };
  if (v === null) return { ...base, note: "Not computed." };
  if (!isFinite(v)) return { ...base, score: 10, note: "No losing trades yet — need a longer window." };
  if (v < 1.0) return { ...base, score: 0, note: "Losing system." };
  if (v < 1.25) return { ...base, score: 6, note: "Edge likely not robust after slippage." };
  if (v < 1.75) return { ...base, score: 12, note: "Marginal — below the 1.75 threshold." };
  if (v < 3.0) return { ...base, score: 18, note: "Healthy profit factor." };
  if (v < 4.0) return { ...base, score: 20, note: "Elite-tier efficiency." };
  return { ...base, score: 10, note: "PF > 4 is rare OOS — assume overfit until proven." };
}

function gradeExpectancySub(v: number | null): SubScore {
  const base: SubScore = { key: "expectancy", label: "Expectancy / trade", score: null, rawValue: v, display: v === null ? "—" : `${(v * 100).toFixed(3)}%`, note: "" };
  if (v === null) return { ...base, note: "Not computed." };
  if (v <= 0) return { ...base, score: 0, note: "Non-positive edge per trade." };
  if (v < 0.001) return { ...base, score: 8, note: "Thin edge — slippage may erase it live." };
  if (v < 0.005) return { ...base, score: 14, note: "Reasonable per-trade edge." };
  if (v < 0.02) return { ...base, score: 20, note: "Strong per-trade edge." };
  return { ...base, score: 18, note: "Very high expectancy — sanity-check the trade log." };
}

function gradeWinRateSub(v: number | null): SubScore {
  const base: SubScore = { key: "win_rate", label: "Win rate", score: null, rawValue: v, display: v === null ? "—" : `${(v * 100).toFixed(2)}%`, note: "" };
  if (v === null) return { ...base, note: "Not computed." };
  if (v < 0.3) return { ...base, score: 4, note: "Very low — needs outsized payoff to compensate." };
  if (v < 0.5) return { ...base, score: 10, note: "Trend-style range — pair with payoff > 2x." };
  if (v < 0.55) return { ...base, score: 14, note: "Below typical mean-reversion range." };
  if (v < 0.75) return { ...base, score: 20, note: "Classic mean-reversion range." };
  if (v < 0.85) return { ...base, score: 16, note: "Very high — confirm with larger sample." };
  return { ...base, score: 6, note: "Above 85% is a data-leak red flag." };
}

function gradePayoffSub(v: number | null): SubScore {
  const base: SubScore = { key: "payoff", label: "Payoff ratio", score: null, rawValue: v, display: v === null ? "—" : v.toFixed(2), note: "" };
  if (v === null) return { ...base, note: "Needs average win + average loss from trade stats." };
  if (v < 0.5) return { ...base, score: 4, note: "Avg loser is 2x the avg winner — fragile edge." };
  if (v < 1.0) return { ...base, score: 10, note: "Below 1.0 — edge depends entirely on hit-rate." };
  if (v < 1.5) return { ...base, score: 15, note: "Typical for mean-reversion." };
  if (v < 3.0) return { ...base, score: 20, note: "Asymmetric winners — robust edge." };
  return { ...base, score: 16, note: "Very asymmetric — verify it's not one whale trade." };
}

function gradeFeeDragSub(netProfit: number | null, fees: number | null): SubScore {
  const base: SubScore = { key: "fee_drag", label: "Fee drag on gross", score: null, rawValue: null, display: "—", note: "" };
  if (netProfit === null || fees === null) return { ...base, note: "Net profit or fee total unavailable." };
  const gross = netProfit + fees;
  if (gross <= 0) return { ...base, score: 0, display: "—", note: "Gross profit non-positive — fees are not the limiting factor." };
  const drag = fees / gross;
  base.rawValue = drag;
  base.display = `${(drag * 100).toFixed(2)}%`;
  if (drag < 0.05) return { ...base, score: 20, note: "Fees barely touch gross profit." };
  if (drag < 0.15) return { ...base, score: 16, note: "Healthy fee efficiency." };
  if (drag < 0.30) return { ...base, score: 11, note: "Fees taking a noticeable bite — stress-test at higher cost." };
  if (drag < 0.50) return { ...base, score: 5, note: "Fees eating half the edge — fragile live." };
  return { ...base, score: 1, note: "Fees dominate — strategy won't survive realistic costs." };
}

function gradePsrSub(v: number | null): SubScore {
  const base: SubScore = { key: "psr", label: "Probabilistic Sharpe", score: null, rawValue: v, display: v === null ? "—" : `${(v * 100).toFixed(2)}%`, note: "" };
  if (v === null) return { ...base, note: "Not yet computed by engine." };
  if (v < 0.5) return { ...base, score: 2, note: "Cannot distinguish strategy from noise." };
  if (v < 0.8) return { ...base, score: 8, note: "Weak statistical confidence." };
  if (v < 0.95) return { ...base, score: 14, note: "Approaching the 95% threshold." };
  if (v < 0.99) return { ...base, score: 20, note: "High statistical confidence." };
  return { ...base, score: 18, note: "Near-certain — verify sample size isn't inflated." };
}

function gradeSampleSizeSub(n: number | null): SubScore {
  const base: SubScore = { key: "sample", label: "Sample size (trades)", score: null, rawValue: n, display: n === null ? "—" : `${n}`, note: "" };
  if (n === null) return { ...base, note: "Trade count unavailable." };
  if (n < 20) return { ...base, score: 2, note: "Too few trades to draw any conclusion." };
  if (n < 50) return { ...base, score: 7, note: "Thin — run on a longer window." };
  if (n < 100) return { ...base, score: 13, note: "Reasonable sample — CI still wide." };
  if (n < 250) return { ...base, score: 18, note: "Robust sample." };
  return { ...base, score: 20, note: "Large sample — statistical power is solid." };
}

function gradeSkepticismSub(sharpe: number | null, pf: number | null, winRate: number | null): SubScore {
  const base: SubScore = { key: "skepticism", label: "Skepticism penalty", score: null, rawValue: null, display: "—", note: "" };
  if (sharpe === null && pf === null && winRate === null) return { ...base, note: "Need at least one of Sharpe, PF, or Win Rate." };
  let score = 20;
  const flags: string[] = [];
  if (sharpe !== null && sharpe > 3.0) { score -= 8; flags.push("Sharpe > 3"); }
  if (pf !== null && isFinite(pf) && pf > 4.0) { score -= 6; flags.push("PF > 4"); }
  if (winRate !== null && winRate > 0.85) { score -= 6; flags.push("Win rate > 85%"); }
  base.score = Math.max(0, score);
  base.display = flags.length === 0 ? "Clean" : flags.join(" · ");
  base.note = flags.length === 0
    ? "None of the skepticism thresholds tripped."
    : `Skeptical thresholds tripped: ${flags.join(", ")}. Verify OOS and check for look-ahead bias.`;
  return base;
}

function gradeTradeGapSub(portfolio: number | null, trade: number | null): SubScore {
  const base: SubScore = { key: "trade_gap", label: "Trade vs Portfolio Sharpe gap", score: null, rawValue: null, display: "—", note: "" };
  if (portfolio === null || trade === null) return { ...base, note: "Needs both Portfolio Sharpe and Trade Sharpe." };
  const gap = trade - portfolio;
  base.rawValue = gap;
  base.display = gap.toFixed(2);
  if (gap < 1.0) return { ...base, score: 20, note: "Low sequencing risk." };
  if (gap < 2.0) return { ...base, score: 16, note: "Modest sequencing risk." };
  if (gap < 3.0) return { ...base, score: 12, note: "Capital spends long periods idle." };
  if (gap < 5.0) return { ...base, score: 6, note: "Elevated sequencing risk." };
  return { ...base, score: 2, note: "Severe gap — performance bursts between long idle periods." };
}

// ----- Helpers --------------------------------------------------------------

function averageSubs(subs: SubScore[]): number | null {
  const scored = subs.filter((s) => typeof s.score === "number");
  if (scored.length === 0) return null;
  const sum = scored.reduce((acc, s) => acc + (s.score ?? 0), 0);
  const maxPossible = scored.length * 20;
  return Math.round((sum / maxPossible) * 100);
}

function num(v: number | null | undefined): number | null {
  if (v === null || v === undefined) return null;
  if (typeof v !== "number" || Number.isNaN(v)) return null;
  return v;
}

function payoffRatio(avgWin: number | null, avgLoss: number | null): number | null {
  if (avgWin === null || avgLoss === null) return null;
  if (avgLoss === 0) return null;
  return Math.abs(avgWin / avgLoss);
}

import type { components } from '../api/broker.types';
import type {
  AlphaDecayStats,
  DataSufficiency,
  DeflatedSharpe,
  EffectiveSampleSize,
  GraduationCriterion,
  GraduationResult,
  GraduationStageInfo,
  Methodology,
  ParameterStability,
  RegimeBucket,
  RegimeCoverageEntry,
  ResearchExperiment,
  SharpeCi,
  SignalBacktestResult,
  SignalBehaviorMetrics,
  SignalDiagnostics,
  SignalEngineResult,
  SignalExperiment,
  Stage0Failure,
  Stage0Rejection,
  StageAdvanceCriterion,
  ThresholdSharpeEntry,
  WalkForwardResult,
  WalkForwardWindow,
} from './research.service';

type Schema = components['schemas'];

/**
 * Python's stored experiment rows and signal reports (snake_case, instants as
 * `*_ms`) to the Research Lab's view models. One mapper per wire shape; each
 * lists every field so a rename on either side fails the type check.
 */

export function toResearchExperiment(row: Schema['ResearchExperimentResponse']): ResearchExperiment {
  return {
    id: row.id,
    ticker: row.ticker,
    featureName: row.feature_name,
    startDate: row.start_date,
    endDate: row.end_date,
    barsUsed: row.bars_used,
    meanIC: row.mean_ic,
    icTStat: row.ic_t_stat,
    icPValue: row.ic_p_value,
    adfPValue: row.adf_p_value,
    kpssPValue: row.kpss_p_value,
    isStationary: row.is_stationary,
    passedValidation: row.passed_validation,
    monotonicityRatio: row.monotonicity_ratio,
    isMonotonic: row.is_monotonic,
    createdAt: row.created_at_ms,
  };
}

export function toSignalExperiment(row: Schema['SignalExperimentResponse']): SignalExperiment {
  return {
    id: row.id,
    ticker: row.ticker,
    featureName: row.feature_name,
    startDate: row.start_date,
    endDate: row.end_date,
    barsUsed: row.bars_used,
    overallGrade: row.overall_grade,
    statusLabel: row.status_label,
    overallPassed: row.overall_passed,
    meanOosSharpe: row.mean_oos_sharpe,
    bestThreshold: row.best_threshold,
    bestCostBps: row.best_cost_bps,
    flipSign: row.flip_sign,
    regimeGateEnabled: row.regime_gate_enabled,
    createdAt: row.created_at_ms,
  };
}

export function toSignalEngineResult(report: Schema['SignalExperimentReport']): SignalEngineResult {
  return {
    success: report.success,
    ticker: report.ticker,
    featureName: report.feature_name,
    startDate: report.start_date,
    endDate: report.end_date,
    barsUsed: report.bars_used,
    flipSign: report.flip_sign,
    thresholdsTested: report.thresholds_tested,
    costBpsOptions: report.cost_bps_options,
    bestThreshold: report.best_threshold,
    bestCostBps: report.best_cost_bps,
    backtestGrid: report.backtest_grid.map(toBacktest),
    walkForward: report.walk_forward ? toWalkForward(report.walk_forward) : null,
    graduation: report.graduation ? toGraduation(report.graduation) : null,
    signalDiagnostics: report.signal_diagnostics ? toDiagnostics(report.signal_diagnostics) : null,
    dataSufficiency: report.data_sufficiency ? toDataSufficiency(report.data_sufficiency) : null,
    effectiveSample: report.effective_sample ? toEffectiveSample(report.effective_sample) : null,
    regimeCoverage: report.regime_coverage.map(toRegimeCount),
    jointRegimeCoverage: report.joint_regime_coverage.map(toRegimeBucket),
    signalBehavior: report.signal_behavior ? toBehavior(report.signal_behavior) : null,
    oosSharpeCi: report.oos_sharpe_ci ? toSharpeCi(report.oos_sharpe_ci) : null,
    deflatedSharpe: report.deflated_sharpe ? toDeflatedSharpe(report.deflated_sharpe) : null,
    methodology: report.methodology ? toMethodology(report.methodology) : null,
    researchLog: report.research_log,
    error: report.error ?? undefined,
  };
}

function toBacktest(b: Schema['SignalReportBacktest']): SignalBacktestResult {
  return {
    threshold: b.threshold,
    costBps: b.cost_bps,
    dates: b.dates,
    cumulativeReturns: b.cumulative_returns,
    positions: b.positions,
    grossSharpe: b.gross_sharpe,
    netSharpe: b.net_sharpe,
    maxDrawdown: b.max_drawdown,
    annualizedTurnover: b.annualized_turnover,
    avgHoldingBars: b.avg_holding_bars,
    winRate: b.win_rate,
    avgWinLossRatio: b.avg_win_loss_ratio,
    totalTrades: b.total_trades,
    netTotalReturn: b.net_total_return,
    grossTotalReturn: b.gross_total_return,
  };
}

function toWalkForward(w: Schema['SignalReportWalkForward']): WalkForwardResult {
  return {
    windows: w.windows.map(toWalkForwardWindow),
    meanOosSharpe: w.mean_oos_sharpe,
    stdOosSharpe: w.std_oos_sharpe,
    medianOosSharpe: w.median_oos_sharpe,
    pctWindowsProfitable: w.pct_windows_profitable,
    pctWindowsPositiveSharpe: w.pct_windows_positive_sharpe,
    worstWindowSharpe: w.worst_window_sharpe,
    bestWindowSharpe: w.best_window_sharpe,
    totalOosBars: w.total_oos_bars,
    combinedOosDates: w.combined_oos_dates,
    combinedOosCumulativeReturns: w.combined_oos_cumulative_returns,
    oosSharpeTrendSlope: w.oos_sharpe_trend_slope,
    alphaDecay: w.alpha_decay ? toAlphaDecay(w.alpha_decay) : null,
  };
}

function toWalkForwardWindow(w: Schema['SignalReportWalkForwardWindow']): WalkForwardWindow {
  return {
    foldIndex: w.fold_index,
    trainStart: w.train_start,
    trainEnd: w.train_end,
    testStart: w.test_start,
    testEnd: w.test_end,
    trainBars: w.train_bars,
    testBars: w.test_bars,
    mu: w.mu,
    sigma: w.sigma,
    bestThreshold: w.best_threshold,
    oosNetSharpe: w.oos_net_sharpe,
    oosGrossSharpe: w.oos_gross_sharpe,
    oosMaxDrawdown: w.oos_max_drawdown,
    oosNetReturn: w.oos_net_return,
    oosWinRate: w.oos_win_rate,
    oosTotalTrades: w.oos_total_trades,
    oosDates: w.oos_dates,
    oosCumulativeReturns: w.oos_cumulative_returns,
  };
}

function toAlphaDecay(a: Schema['SignalReportAlphaDecay']): AlphaDecayStats {
  return {
    slope: a.slope,
    intercept: a.intercept,
    tStat: a.t_stat,
    pValue: a.p_value,
    rSquared: a.r_squared,
    nFoldsUsed: a.n_folds_used,
    isTestValid: a.is_test_valid,
    isSignificant: a.is_significant,
  };
}

function toGraduation(g: Schema['SignalReportGraduation']): GraduationResult {
  return {
    criteria: g.criteria.map(toGraduationCriterion),
    overallPassed: g.overall_passed,
    overallGrade: g.overall_grade,
    summary: g.summary,
    statusLabel: g.status_label,
    parameterStability: g.parameter_stability ? toParameterStability(g.parameter_stability) : null,
    stage0Rejection: g.stage0_rejection ? toStage0Rejection(g.stage0_rejection) : null,
    stageInfo: g.stage_info ? toStageInfo(g.stage_info) : null,
  };
}

function toGraduationCriterion(c: Schema['SignalReportGraduationCriterion']): GraduationCriterion {
  return {
    name: c.name,
    description: c.description,
    passed: c.passed,
    value: c.value,
    threshold: c.threshold,
    label: c.label,
    failureReason: c.failure_reason,
  };
}

function toParameterStability(p: Schema['SignalReportParameterStability']): ParameterStability {
  return {
    sharpeValuesByThreshold: p.sharpe_values_by_threshold.map(
      (entry): ThresholdSharpeEntry => ({ threshold: entry.threshold, sharpe: entry.sharpe }),
    ),
    stabilityScore: p.stability_score,
    stabilityLabel: p.stability_label,
  };
}

function toStage0Rejection(r: Schema['SignalReportStage0Rejection']): Stage0Rejection {
  return {
    rejected: r.rejected,
    failedCriteria: r.failed_criteria.map(
      (f): Stage0Failure => ({
        criterionName: f.criterion_name,
        value: f.value,
        thresholdRepr: f.threshold_repr,
        message: f.message,
      }),
    ),
  };
}

function toStageInfo(s: Schema['SignalReportStageInfo']): GraduationStageInfo {
  return {
    stage: toStage(s.stage),
    label: s.label,
    description: s.description,
    nextStageLabel: s.next_stage_label,
    advanceCriteria: s.advance_criteria.map(toStageAdvanceCriterion),
  };
}

function toStageAdvanceCriterion(c: Schema['SignalReportStageAdvanceCriterion']): StageAdvanceCriterion {
  return {
    name: c.name,
    description: c.description,
    currentValue: c.current_value,
    requiredRepr: c.required_repr,
    met: c.met,
  };
}

/** The ladder has four rungs; a stored stage off it is a corrupt report, not a rung to guess. */
function toStage(stage: number): GraduationStageInfo['stage'] {
  if (stage === 0 || stage === 1 || stage === 2 || stage === 3) return stage;
  throw new Error(`The stored signal report names graduation stage ${stage}, which is not 0 to 3`);
}

function toDiagnostics(d: Schema['SignalReportDiagnostics']): SignalDiagnostics {
  return {
    signalMean: d.signal_mean,
    signalStd: d.signal_std,
    pctTimeActive: d.pct_time_active,
    avgAbsSignal: d.avg_abs_signal,
    pctFilteredByThreshold: d.pct_filtered_by_threshold,
    pctGatedByRegime: d.pct_gated_by_regime,
  };
}

function toDataSufficiency(d: Schema['SignalReportDataSufficiency']): DataSufficiency {
  return {
    totalBars: d.total_bars,
    trainBars: d.train_bars,
    testBars: d.test_bars,
    walkForwardFolds: d.walk_forward_folds,
    effectiveOosBars: d.effective_oos_bars,
    regimesCovered: d.regimes_covered,
    regimeCoverage: d.regime_coverage.map(toRegimeCount),
    coverageWarnings: d.coverage_warnings,
  };
}

function toRegimeCount(r: Schema['SignalReportRegimeCount']): RegimeCoverageEntry {
  return { regime: r.regime, count: r.count };
}

function toEffectiveSample(e: Schema['SignalReportEffectiveSample']): EffectiveSampleSize {
  return {
    rawN: e.raw_n,
    effectiveN: e.effective_n,
    autocorrelationLag1: e.autocorrelation_lag1,
    independentBets: e.independent_bets,
    maxLagUsed: e.max_lag_used,
    rhoSum: e.rho_sum,
  };
}

function toRegimeBucket(b: Schema['SignalReportRegimeBucket']): RegimeBucket {
  return {
    volLabel: b.vol_label,
    trendLabel: b.trend_label,
    days: b.days,
    effectiveTrades: b.effective_trades,
    badge: b.badge,
  };
}

function toBehavior(b: Schema['SignalReportBehavior']): SignalBehaviorMetrics {
  return {
    avgForwardReturnWhenActive: b.avg_forward_return_when_active,
    skewnessActiveReturns: b.skewness_active_returns,
    avgWinReturn: b.avg_win_return,
    avgLossReturn: b.avg_loss_return,
    hitRate: b.hit_rate,
  };
}

function toSharpeCi(c: Schema['SignalReportSharpeCi']): SharpeCi {
  return {
    method: c.method ?? 'legacy_neff',
    sampleSize: c.sample_size ?? 0,
    hacLag: c.hac_lag ?? 0,
    segmentCount: c.segment_count ?? 0,
    unavailableReason: c.unavailable_reason ?? '',
    point: c.point,
    se: c.se,
    ciLower: c.ci_lower,
    ciUpper: c.ci_upper,
    confidenceLevel: c.confidence_level,
    nEffUsed: c.n_eff_used,
    valid: c.valid,
  };
}

function toDeflatedSharpe(d: Schema['SignalReportDeflatedSharpe']): DeflatedSharpe {
  return {
    method: d.method ?? 'legacy_proxy',
    sampleSize: d.sample_size ?? 0,
    effectiveTrials: d.effective_trials ?? 0,
    trialSharpeStd: d.trial_sharpe_std ?? 0,
    meanTrialCorrelation: d.mean_trial_correlation ?? 0,
    unavailableReason: d.unavailable_reason ?? '',
    rawSharpe: d.raw_sharpe,
    expectedMaxUnderNull: d.expected_max_under_null,
    dsrProbability: d.dsr_probability,
    nTrials: d.n_trials,
    skewness: d.skewness,
    kurtosis: d.kurtosis,
    valid: d.valid,
  };
}

function toMethodology(m: Schema['SignalReportMethodology']): Methodology {
  return {
    trainMonths: m.train_months,
    testMonths: m.test_months,
    windowType: m.window_type,
    optimizationTarget: m.optimization_target,
    annualizationFactor: m.annualization_factor,
    barsPerDay: m.bars_per_day,
    horizon: m.horizon,
    defaultCostBps: m.default_cost_bps,
    minBarsForSignal: m.min_bars_for_signal,
    flipSign: m.flip_sign,
    regimeGateEnabled: m.regime_gate_enabled,
    thresholds: m.thresholds,
    costBpsOptions: m.cost_bps_options,
  };
}

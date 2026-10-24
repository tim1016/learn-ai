import {
  SignalEngineResult,
  SignalBacktestResult,
  WalkForwardResult,
  WalkForwardWindow,
  AlphaDecayStats,
  GraduationResult,
  GraduationCriterion,
  ParameterStability,
  ThresholdSharpeEntry,
  Stage0Rejection,
  Stage0Failure,
  GraduationStageInfo,
  StageAdvanceCriterion,
  SignalDiagnostics,
  DataSufficiency,
  RegimeCoverageEntry,
  EffectiveSampleSize,
  SignalBehaviorMetrics,
  SharpeCi,
  DeflatedSharpe,
  RegimeBucket,
  Methodology,
} from '../../../services/research.service';

/** Snake-case shape returned by /api/jobs-internal/signal-engine worker. */
export interface SignalEngineJobResultRaw {
  success: boolean;
  ticker: string;
  feature_name: string;
  start_date: string;
  end_date: string;
  bars_used: number;
  flip_sign: boolean;
  thresholds_tested: number[];
  cost_bps_options: number[];
  best_threshold: number;
  best_cost_bps: number;
  backtest_grid: unknown[];
  walk_forward: unknown;
  graduation: unknown;
  signal_diagnostics: unknown;
  data_sufficiency: unknown;
  effective_sample: unknown;
  regime_coverage: Record<string, number>;
  joint_regime_coverage: unknown[];
  signal_behavior: unknown;
  oos_sharpe_ci: unknown;
  deflated_sharpe: unknown;
  methodology: unknown;
  research_log: string;
  error: string | null;
}

/** Transform snake_case worker payload → camelCase SignalEngineResult. */
export function toSignalEngineResultFromJob(raw: SignalEngineJobResultRaw): SignalEngineResult {
  const regimeCoverageEntries = Object.entries(raw.regime_coverage ?? {}).map(
    ([regime, count]) => ({ regime, count }),
  );
  return {
    success: raw.success,
    ticker: raw.ticker,
    featureName: raw.feature_name,
    startDate: raw.start_date,
    endDate: raw.end_date,
    barsUsed: raw.bars_used,
    flipSign: raw.flip_sign,
    thresholdsTested: raw.thresholds_tested ?? [],
    costBpsOptions: raw.cost_bps_options ?? [],
    bestThreshold: raw.best_threshold,
    bestCostBps: raw.best_cost_bps,
    backtestGrid: mapBacktestGrid(raw.backtest_grid),
    walkForward: raw.walk_forward
      ? mapWalkForward(raw.walk_forward as Record<string, unknown>)
      : null,
    graduation: raw.graduation
      ? mapGraduation(raw.graduation as Record<string, unknown>)
      : null,
    signalDiagnostics: raw.signal_diagnostics
      ? mapSignalDiagnostics(raw.signal_diagnostics as Record<string, unknown>)
      : null,
    dataSufficiency: raw.data_sufficiency
      ? mapDataSufficiency(raw.data_sufficiency as Record<string, unknown>)
      : null,
    effectiveSample: raw.effective_sample
      ? mapEffectiveSample(raw.effective_sample as Record<string, unknown>)
      : null,
    regimeCoverage: regimeCoverageEntries,
    jointRegimeCoverage: mapJointRegimeCoverage(raw.joint_regime_coverage),
    signalBehavior: raw.signal_behavior
      ? mapSignalBehavior(raw.signal_behavior as Record<string, unknown>)
      : null,
    oosSharpeCi: raw.oos_sharpe_ci
      ? mapSharpeCi(raw.oos_sharpe_ci as Record<string, unknown>)
      : null,
    deflatedSharpe: raw.deflated_sharpe
      ? mapDeflatedSharpe(raw.deflated_sharpe as Record<string, unknown>)
      : null,
    methodology: raw.methodology
      ? mapMethodology(raw.methodology as Record<string, unknown>)
      : null,
    researchLog: raw.research_log ?? '',
    error: raw.error ?? undefined,
  };
}

function mapBacktestGrid(raw: unknown[]): SignalBacktestResult[] {
  if (!Array.isArray(raw)) return [];
  return (raw as Record<string, unknown>[]).map(b => ({
    threshold: b['threshold'] as number,
    costBps: b['cost_bps'] as number,
    dates: (b['dates'] as string[]) ?? [],
    cumulativeReturns: (b['cumulative_returns'] as number[]) ?? [],
    positions: (b['positions'] as number[]) ?? [],
    grossSharpe: b['gross_sharpe'] as number,
    netSharpe: b['net_sharpe'] as number,
    maxDrawdown: b['max_drawdown'] as number,
    annualizedTurnover: b['annualized_turnover'] as number,
    avgHoldingBars: b['avg_holding_bars'] as number,
    winRate: b['win_rate'] as number,
    avgWinLossRatio: b['avg_win_loss_ratio'] as number,
    totalTrades: b['total_trades'] as number,
    netTotalReturn: b['net_total_return'] as number,
    grossTotalReturn: b['gross_total_return'] as number,
  }));
}

function mapAlphaDecay(r: Record<string, unknown>): AlphaDecayStats {
  return {
    slope: r['slope'] as number,
    intercept: r['intercept'] as number,
    tStat: r['t_stat'] as number,
    pValue: r['p_value'] as number,
    rSquared: r['r_squared'] as number,
    nFoldsUsed: r['n_folds_used'] as number,
    isTestValid: r['is_test_valid'] as boolean,
    isSignificant: r['is_significant'] as boolean,
  };
}

function mapWalkForwardWindow(r: Record<string, unknown>): WalkForwardWindow {
  return {
    foldIndex: r['fold_index'] as number,
    trainStart: r['train_start'] as string,
    trainEnd: r['train_end'] as string,
    testStart: r['test_start'] as string,
    testEnd: r['test_end'] as string,
    trainBars: r['train_bars'] as number,
    testBars: r['test_bars'] as number,
    mu: r['mu'] as number,
    sigma: r['sigma'] as number,
    bestThreshold: r['best_threshold'] as number,
    oosNetSharpe: r['oos_net_sharpe'] as number,
    oosGrossSharpe: r['oos_gross_sharpe'] as number,
    oosMaxDrawdown: r['oos_max_drawdown'] as number,
    oosNetReturn: r['oos_net_return'] as number,
    oosWinRate: r['oos_win_rate'] as number,
    oosTotalTrades: r['oos_total_trades'] as number,
    oosDates: (r['oos_dates'] as string[]) ?? [],
    oosCumulativeReturns: (r['oos_cumulative_returns'] as number[]) ?? [],
  };
}

function mapWalkForward(r: Record<string, unknown>): WalkForwardResult {
  const windows = (r['windows'] as Record<string, unknown>[]) ?? [];
  const ad = r['alpha_decay'] as Record<string, unknown> | null;
  return {
    windows: windows.map(w => mapWalkForwardWindow(w)),
    meanOosSharpe: r['mean_oos_sharpe'] as number,
    stdOosSharpe: r['std_oos_sharpe'] as number,
    medianOosSharpe: r['median_oos_sharpe'] as number,
    pctWindowsProfitable: r['pct_windows_profitable'] as number,
    pctWindowsPositiveSharpe: r['pct_windows_positive_sharpe'] as number,
    worstWindowSharpe: r['worst_window_sharpe'] as number,
    bestWindowSharpe: r['best_window_sharpe'] as number,
    totalOosBars: r['total_oos_bars'] as number,
    combinedOosDates: (r['combined_oos_dates'] as string[]) ?? [],
    combinedOosCumulativeReturns: (r['combined_oos_cumulative_returns'] as number[]) ?? [],
    oosSharpeTrendSlope: r['oos_sharpe_trend_slope'] as number,
    alphaDecay: ad ? mapAlphaDecay(ad) : null,
  };
}

function mapGraduationCriterion(r: Record<string, unknown>): GraduationCriterion {
  return {
    name: r['name'] as string,
    description: r['description'] as string,
    passed: r['passed'] as boolean,
    value: r['value'] as number,
    threshold: r['threshold'] as number,
    label: r['label'] as string,
    failureReason: r['failure_reason'] as string,
  };
}

function mapStageAdvanceCriterion(r: Record<string, unknown>): StageAdvanceCriterion {
  return {
    name: r['name'] as string,
    description: r['description'] as string,
    currentValue: r['current_value'] as number,
    requiredRepr: r['required_repr'] as string,
    met: r['met'] as boolean,
  };
}

function mapGraduation(r: Record<string, unknown>): GraduationResult {
  const criteria = (r['criteria'] as Record<string, unknown>[]) ?? [];
  const ps = r['parameter_stability'] as Record<string, unknown> | null;
  const s0 = r['stage0_rejection'] as Record<string, unknown> | null;
  const si = r['stage_info'] as Record<string, unknown> | null;

  const parameterStability: ParameterStability | null = ps
    ? {
        sharpeValuesByThreshold: Object.entries(
          (ps['sharpe_values_by_threshold'] as Record<string, number>) ?? {},
        ).map(([threshold, sharpe]): ThresholdSharpeEntry => ({ threshold: Number(threshold), sharpe })),
        stabilityScore: ps['stability_score'] as number,
        stabilityLabel: ps['stability_label'] as string,
      }
    : null;

  const stage0Rejection: Stage0Rejection | null = s0
    ? {
        rejected: s0['rejected'] as boolean,
        failedCriteria: ((s0['failed_criteria'] as Record<string, unknown>[]) ?? []).map(
          (f): Stage0Failure => ({
            criterionName: f['criterion_name'] as string,
            value: f['value'] as number,
            thresholdRepr: f['threshold_repr'] as string,
            message: f['message'] as string,
          }),
        ),
      }
    : null;

  const stageInfo: GraduationStageInfo | null = si
    ? {
        stage: si['stage'] as 0 | 1 | 2 | 3,
        label: si['label'] as string,
        description: si['description'] as string,
        nextStageLabel: si['next_stage_label'] as string,
        advanceCriteria: ((si['advance_criteria'] as Record<string, unknown>[]) ?? []).map(
          c => mapStageAdvanceCriterion(c),
        ),
      }
    : null;

  return {
    criteria: criteria.map(c => mapGraduationCriterion(c)),
    overallPassed: r['overall_passed'] as boolean,
    overallGrade: r['overall_grade'] as string,
    summary: r['summary'] as string,
    statusLabel: r['status_label'] as string,
    parameterStability,
    stage0Rejection,
    stageInfo,
  };
}

function mapSignalDiagnostics(r: Record<string, unknown>): SignalDiagnostics {
  return {
    signalMean: r['signal_mean'] as number,
    signalStd: r['signal_std'] as number,
    pctTimeActive: r['pct_time_active'] as number,
    avgAbsSignal: r['avg_abs_signal'] as number,
    pctFilteredByThreshold: r['pct_filtered_by_threshold'] as number,
    pctGatedByRegime: r['pct_gated_by_regime'] as number,
  };
}

function mapDataSufficiency(r: Record<string, unknown>): DataSufficiency {
  // The job writes the coverage as a count per regime, as it does at the top of the result.
  const rc = Object.entries((r['regime_coverage'] as Record<string, number> | null) ?? {});
  return {
    totalBars: r['total_bars'] as number,
    trainBars: r['train_bars'] as number,
    testBars: r['test_bars'] as number,
    walkForwardFolds: r['walk_forward_folds'] as number,
    effectiveOosBars: r['effective_oos_bars'] as number,
    regimesCovered: r['regimes_covered'] as number,
    regimeCoverage: rc.map(([regime, count]): RegimeCoverageEntry => ({ regime, count })),
    coverageWarnings: (r['coverage_warnings'] as string[]) ?? [],
  };
}

function mapEffectiveSample(r: Record<string, unknown>): EffectiveSampleSize {
  return {
    rawN: r['raw_n'] as number,
    effectiveN: r['effective_n'] as number,
    autocorrelationLag1: r['autocorrelation_lag1'] as number,
    independentBets: r['independent_bets'] as number,
    maxLagUsed: r['max_lag_used'] as number,
    rhoSum: r['rho_sum'] as number,
  };
}

function mapJointRegimeCoverage(raw: unknown[]): RegimeBucket[] {
  if (!Array.isArray(raw)) return [];
  return (raw as Record<string, unknown>[]).map(b => ({
    volLabel: b['vol_label'] as string,
    trendLabel: b['trend_label'] as string,
    days: b['days'] as number,
    effectiveTrades: b['effective_trades'] as number,
    badge: b['badge'] as string,
  }));
}

function mapSignalBehavior(r: Record<string, unknown>): SignalBehaviorMetrics {
  return {
    avgForwardReturnWhenActive: r['avg_forward_return_when_active'] as number,
    skewnessActiveReturns: r['skewness_active_returns'] as number,
    avgWinReturn: r['avg_win_return'] as number,
    avgLossReturn: r['avg_loss_return'] as number,
    hitRate: r['hit_rate'] as number,
  };
}

function mapSharpeCi(r: Record<string, unknown>): SharpeCi {
  return {
    method: (r['method'] as string | undefined) ?? 'legacy_neff',
    sampleSize: (r['sample_size'] as number | undefined) ?? 0,
    hacLag: (r['hac_lag'] as number | undefined) ?? 0,
    segmentCount: (r['segment_count'] as number | undefined) ?? 0,
    unavailableReason: (r['unavailable_reason'] as string | undefined) ?? '',
    point: r['point'] as number,
    se: r['se'] as number,
    ciLower: r['ci_lower'] as number,
    ciUpper: r['ci_upper'] as number,
    confidenceLevel: r['confidence_level'] as number,
    nEffUsed: r['n_eff_used'] as number,
    valid: r['valid'] as boolean,
  };
}

function mapDeflatedSharpe(r: Record<string, unknown>): DeflatedSharpe {
  return {
    method: (r['method'] as string | undefined) ?? 'legacy_proxy',
    sampleSize: (r['sample_size'] as number | undefined) ?? 0,
    effectiveTrials: (r['effective_trials'] as number | undefined) ?? 0,
    trialSharpeStd: (r['trial_sharpe_std'] as number | undefined) ?? 0,
    meanTrialCorrelation: (r['mean_trial_correlation'] as number | undefined) ?? 0,
    unavailableReason: (r['unavailable_reason'] as string | undefined) ?? '',
    rawSharpe: r['raw_sharpe'] as number,
    expectedMaxUnderNull: r['expected_max_under_null'] as number,
    dsrProbability: r['dsr_probability'] as number,
    nTrials: r['n_trials'] as number,
    skewness: r['skewness'] as number,
    kurtosis: r['kurtosis'] as number,
    valid: r['valid'] as boolean,
  };
}

function mapMethodology(r: Record<string, unknown>): Methodology {
  return {
    trainMonths: r['train_months'] as number,
    testMonths: r['test_months'] as number,
    windowType: r['window_type'] as string,
    optimizationTarget: r['optimization_target'] as string,
    annualizationFactor: r['annualization_factor'] as number,
    barsPerDay: r['bars_per_day'] as number,
    horizon: r['horizon'] as number,
    defaultCostBps: r['default_cost_bps'] as number,
    minBarsForSignal: r['min_bars_for_signal'] as number,
    flipSign: r['flip_sign'] as boolean,
    regimeGateEnabled: r['regime_gate_enabled'] as boolean,
    thresholds: (r['thresholds'] as number[] | null) ?? null,
    costBpsOptions: (r['cost_bps_options'] as number[] | null) ?? null,
  };
}

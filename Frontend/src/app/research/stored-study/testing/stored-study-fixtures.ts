import { lakeDataSource } from '../../../components/edge/testing/edge-compute-world';
import type { StoredStudyRecord, StoredStudyType } from '../stored-study';

/**
 * Saved runs as the data plane serves them, for the specs of everything that reads one. Each `*_RESULT` is the study's
 * own result in the shape its job or route delivers it (snake_case), small enough to read and complete enough that
 * the study's own mapper and result view draw it.
 */
export const RECORD_ID = 'a'.repeat(32);
export const PARENT_ID = 'b'.repeat(32);
export const SAVED_AT_MS = 1_790_000_000_000;

const DATED = { symbol: 'SPY', from_date: '2024-01-02', to_date: '2024-03-28' };

export const FEATURE_INPUTS = { ...DATED, timespan: 'minute', multiplier: 5, session: 'extended', feature_name: 'rsi_14' };
export const SIGNAL_INPUTS = {
  ...DATED,
  timespan: 'minute',
  multiplier: 15,
  session: 'extended',
  feature_name: 'macd_signal',
  flip_sign: false,
  regime_gate_enabled: true,
};
export const CROSS_INPUTS = {
  symbols: ['SPY', 'QQQ', 'AAPL'],
  from_date: '2025-09-02',
  to_date: '2025-12-01',
  timespan: 'day',
  multiplier: 1,
  session: 'rth',
  feature_name: 'iv_rank_60',
  target_type: 'directional',
};
export const RELIABILITY_INPUTS = {
  ...DATED,
  timespan: 'minute',
  multiplier: 1,
  session: 'rth',
  indicator_name: 'rsi',
  indicator_params: { length: 14 },
  horizons: [5, 1],
  include_slope: false,
};
export const RVIV_INPUTS = {
  symbol: 'SPY',
  bar_size: '1d',
  tenor_days: 30,
  hf_session: 'ETH',
  session: null,
  estimators: ['yz'],
  windows: [5, 10, 30],
  confidence_floor: 0.5,
  window_start_ms: 1_704_171_600_000,
  window_end_ms: 1_711_598_400_000,
  iv_series_supplied: false,
};

export const FEATURE_RESULT = {
  success: true,
  ticker: 'SPY',
  feature_name: 'rsi_14',
  start_date: '2024-01-02',
  end_date: '2024-03-28',
  bars_used: 31_250,
  mean_ic: 0.0123,
  ic_t_stat: 2.4,
  ic_p_value: 0.02,
  nw_t_stat: 2.1,
  nw_p_value: 0.04,
  effective_n: 58,
  ic_values: [],
  ic_dates: [],
  adf_pvalue: 0.01,
  kpss_pvalue: 0.1,
  is_stationary: true,
  quantile_bins: [],
  is_monotonic: false,
  monotonicity_ratio: 0.5,
  robustness: null,
  feature_spec: null,
  validation_verdict: null,
  target: null,
  passed_validation: true,
  error: null,
};

export const SIGNAL_RESULT = {
  success: true,
  ticker: 'SPY',
  feature_name: 'macd_signal',
  start_date: '2024-01-02',
  end_date: '2024-03-28',
  bars_used: 9_000,
  flip_sign: false,
  thresholds_tested: [1],
  cost_bps_options: [2],
  best_threshold: 1,
  best_cost_bps: 2,
  backtest_grid: [],
  walk_forward: null,
  graduation: null,
  signal_diagnostics: null,
  data_sufficiency: null,
  effective_sample: null,
  regime_coverage: {},
  joint_regime_coverage: [],
  signal_behavior: null,
  oos_sharpe_ci: null,
  deflated_sharpe: null,
  methodology: null,
  research_log: '',
  error: null,
};

function member(ticker: string, extra: Record<string, unknown> = {}) {
  return {
    ticker,
    mean_ic: 0.02,
    ic_t_stat: 1.1,
    ic_p_value: 0.2,
    nw_t_stat: 1,
    nw_p_value: 0.3,
    effective_n: 40,
    is_stationary: true,
    passed_validation: true,
    data_points: 60,
    error: null,
    validity: 'valid',
    ...extra,
  };
}

export const CROSS_RESULT = {
  success: true,
  feature_name: 'iv_rank_60',
  target_type: 'directional',
  tickers_tested: 3,
  tickers_tested_raw: 3,
  tickers_valid: 2,
  tickers_passed: 1,
  pass_rate: 0.5,
  cross_sectional_consistent: false,
  aggregate_ic: 0.0213,
  validity_summary: { valid: 2, invalid_iv: 0, invalid_data: 0, errored: 1 },
  stage_info: {
    stage: 1,
    label: 'Weak',
    description: 'Some members pass, the universe does not.',
    next_stage_label: 'Research candidate',
    failed_criteria: [],
    advance_criteria: [],
  },
  ticker_results: [
    member('SPY'),
    member('QQQ', { passed_validation: false }),
    member('AAPL', { validity: 'error', passed_validation: false, error: 'Option history request timed out.' }),
  ],
  summary: '1 of 2 valid members passed.',
};

function horizon(h: number, isIc: number) {
  return {
    horizon: h,
    is_mean_ic: isIc,
    is_t_stat: 1.1,
    is_p_value: 0.3,
    is_nw_t_stat: 1,
    is_nw_p_value: 0.32,
    is_effective_n: 300,
    oos_mean_ic: isIc * 0.9,
    oos_t_stat: 0.9,
    oos_p_value: 0.4,
    oos_effective_n: 120,
    oos_retention: 0.75,
    bonferroni_p: 0.9,
    fdr_p: 0.4,
    random_baseline_mean: 0,
    random_baseline_std: 0.01,
    ic_vs_random_zscore: 1.2,
    is_interpretation: '',
    oos_interpretation: null,
    is_hit_rate: 0.51,
    is_daily_ic_std: 0.05,
    strength_label: 'Weak',
    stability_label: 'Moderate',
    direction_label: 'Momentum',
    retention_delta_pct: -10,
    slope_adds_value: null,
    slope_recommended: null,
    annualized_ir: 0.4,
    sharpe_estimate: 0.3,
    breadth_per_year: 250,
    random_baseline_distribution: [],
  };
}

export const RELIABILITY_RESULT = {
  success: true,
  ticker: 'SPY',
  indicator_name: 'rsi',
  indicator_params: { length: 14 },
  display_name: 'RSI (14)',
  category: 'momentum',
  start_date: '2024-01-02',
  end_date: '2024-03-28',
  bar_count: 12_345,
  train_start: '2024-01-02',
  train_end: '2024-02-20',
  test_start: '2024-02-21',
  test_end: '2024-03-28',
  train_bars: 8_000,
  test_bars: 4_345,
  train_ratio: 0.7,
  results: [horizon(1, 0.02), horizon(5, 0.12)],
  slope_results: null,
  daily_ic_values: [],
  daily_ic_dates: [],
  best_horizon: 5,
  any_significant_after_bonferroni: false,
  any_significant_after_fdr: false,
  num_horizons_tested: 2,
  random_simulations: 100,
  verdict: null,
  decay_curve: [],
  regime_results: null,
  next_steps: [],
  info_footnotes: [],
  warnings: [],
  error: null,
};

const N = 6;
const FIRST_BAR_MS = 1_704_205_200_000;
const flat = (value: number | null) => Array.from({ length: N }, () => value);

export const RVIV_RESULT = {
  symbol: 'SPY',
  ts: Array.from({ length: N }, (_, i) => FIRST_BAR_MS + i * 86_400_000),
  rv_trailing: { yz_5: flat(0.14), yz_10: flat(0.14), yz_30: flat(0.14) },
  rv_forward: { yz_5: flat(0.14), yz_10: flat(0.14), yz_30: flat(0.14) },
  iv30: flat(null),
  iv30_trd252: flat(null),
  rv_hf_trailing: flat(0.14),
  rv_hf_forward: [...flat(0.15).slice(0, N - 2), null, null],
  vrp_forward: flat(null),
  vrp_z: flat(null),
  iv_source: 'absent',
  bar_size: '1d',
  tenor_days: 30,
  hf_session: null,
  coverage: {
    n_bars: N,
    iv_first_ts: null,
    iv_last_ts: null,
    forward_nan_bars: 2,
    vrp_lookback: 4,
    session: null,
    iv_alignment: { rule: 'snapshot_in_bar_latest', snapshots_read: 0, bars_with_iv: 0, superseded: 0, outside_bars: 0 },
  },
  bars: {
    open: Array.from({ length: N }, (_, i) => 500 + i),
    high: Array.from({ length: N }, (_, i) => 503 + i),
    low: Array.from({ length: N }, (_, i) => 498 + i),
    close: Array.from({ length: N }, (_, i) => 501 + i),
    volume: Array.from({ length: N }, () => 1_000_000),
  },
  data_source: {
    ...lakeDataSource(),
    iv_source: 'absent',
    iv_snapshots: 0,
    iv_digest: 'c'.repeat(64),
  },
};

const INPUTS: Record<StoredStudyType, Record<string, never>> = {
  'feature-study': FEATURE_INPUTS as never,
  'signal-study': SIGNAL_INPUTS as never,
  'cross-sectional-study': CROSS_INPUTS as never,
  'indicator-reliability': RELIABILITY_INPUTS as never,
  'realized-vs-iv': RVIV_INPUTS as never,
};

const RESULTS: Record<StoredStudyType, Record<string, never>> = {
  'feature-study': FEATURE_RESULT as never,
  'signal-study': SIGNAL_RESULT as never,
  'cross-sectional-study': CROSS_RESULT as never,
  'indicator-reliability': RELIABILITY_RESULT as never,
  'realized-vs-iv': RVIV_RESULT as never,
};

const QUESTIONS: Record<StoredStudyType, string> = {
  'feature-study': 'Does rsi_14 predict the next move of SPY?',
  'signal-study': 'Does a trading signal built on macd_signal hold up out of sample on SPY?',
  'cross-sectional-study': "Does iv_rank_60 predict the next day's direction across 3 instruments?",
  'indicator-reliability': 'Is rsi(length=14) on SPY a reliable predictor of what price does next?',
  'realized-vs-iv': 'Is implied volatility richer or cheaper than the volatility SPY then realizes? (30-day tenor, 1d bars)',
};

/** A completed saved run of `study`, with the changes a test needs. */
export function storedRecord(study: StoredStudyType, changes: Partial<StoredStudyRecord> = {}): StoredStudyRecord {
  const universe = study === 'cross-sectional-study';
  return {
    study,
    record_id: RECORD_ID,
    schema_version: 1,
    status: 'completed',
    status_detail: null,
    question: QUESTIONS[study],
    symbols: universe ? ['SPY', 'QQQ', 'AAPL'] : ['SPY'],
    window: { start_ms: 1_704_171_600_000, end_ms: 1_711_598_400_000 },
    created_at_ms: SAVED_AT_MS,
    completed_at_ms: SAVED_AT_MS + 5_000,
    inputs: INPUTS[study],
    data_source: null,
    data_identity: null,
    job_id: null,
    rerun_of: null,
    result: RESULTS[study],
    ...changes,
  };
}

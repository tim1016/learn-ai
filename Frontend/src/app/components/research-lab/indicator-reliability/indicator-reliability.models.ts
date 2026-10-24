import type { components } from '../../../api/broker.types';

// ─── Wire shapes of POST /api/research/indicator-reliability ───────────────

/** The `data_source` of a reliability result: the shared lake block plus the warmup report (#3523). */
export type ReliabilityDataSource = components['schemas']['ReliabilityDataSource-Output'];

/** The sessions a run asks the lake for, warmup lead-in and window, as the server sizes them (#3523). */
export type ReliabilitySpan = components['schemas']['ReliabilitySpan'];

export type StrengthLabel = 'Noise' | 'Weak' | 'Moderate' | 'Strong';
export type StabilityLabel = 'Low' | 'Moderate' | 'High';
export type DirectionLabel = 'Mean-Reversion' | 'Momentum' | 'None';
export type TradeabilityLabel = 'Likely tradeable' | 'Marginal' | 'Unlikely' | 'Unknown';

export interface Verdict {
  direction: DirectionLabel;
  strength: StrengthLabel;
  stability: StabilityLabel;
  tradeability: TradeabilityLabel;
  horizon: number | null;
  tradeability_caveat: string | null;
}

export interface DecayCurvePoint {
  horizon: number;
  ic: number;
  p_value: number;
  ic_stderr: number;
}

export interface RegimeICPoint {
  horizon: number;
  mean_ic: number;
  t_stat: number;
  p_value: number;
  effective_n: number;
  hit_rate: number;
  bars_in_regime: number;
}

export interface RegimeResults {
  high_vol: RegimeICPoint[] | null;
  low_vol: RegimeICPoint[] | null;
  vol_window: number;
}

export interface HorizonICResult {
  horizon: number;
  // In-sample
  is_mean_ic: number;
  is_t_stat: number;
  is_p_value: number;
  is_nw_t_stat: number | null;
  is_nw_p_value: number | null;
  is_effective_n: number;
  // Out-of-sample
  oos_mean_ic: number | null;
  oos_t_stat: number | null;
  oos_p_value: number | null;
  oos_effective_n: number | null;
  oos_retention: number | null;
  // Multiple testing
  bonferroni_p: number;
  fdr_p: number;
  // Random baseline
  random_baseline_mean: number;
  random_baseline_std: number;
  ic_vs_random_zscore: number;
  // Interpretations (legacy)
  is_interpretation: string;
  oos_interpretation: string | null;
  // Stability
  is_hit_rate: number;
  is_daily_ic_std: number;
  // Verdict labels
  strength_label: StrengthLabel;
  stability_label: StabilityLabel;
  direction_label: DirectionLabel;
  // OOS delta as +% / -%, null if OOS missing
  retention_delta_pct: number | null;
  // Slope decision flags (populated on slope variant rows only)
  slope_adds_value: boolean | null;
  slope_recommended: boolean | null;
  // IR proxy
  annualized_ir: number;
  sharpe_estimate: number;
  breadth_per_year: number;
  // Random baseline distribution (populated only for best horizon)
  random_baseline_distribution: number[];
}

export interface IndicatorReliabilityResponse {
  success: boolean;
  ticker: string;
  indicator_name: string;
  indicator_params: Record<string, number>;
  display_name: string;
  category: string | null;
  start_date: string;
  end_date: string;
  bar_count: number;
  // Train/test split
  train_start: string | null;
  train_end: string | null;
  test_start: string | null;
  test_end: string | null;
  train_bars: number | null;
  test_bars: number | null;
  train_ratio: number;
  // Results
  results: HorizonICResult[];
  slope_results: HorizonICResult[] | null;
  daily_ic_values: number[];
  daily_ic_dates: string[];
  best_horizon: number | null;
  // Multiple testing
  any_significant_after_bonferroni: boolean;
  any_significant_after_fdr: boolean;
  num_horizons_tested: number;
  random_simulations: number;
  // Top-line verdict
  verdict: Verdict | null;
  // P2 diagnostics
  decay_curve: DecayCurvePoint[];
  regime_results: RegimeResults | null;
  // P3 economic layer & honesty
  next_steps: string[];
  info_footnotes: string[];
  // Warnings
  warnings: string[];
  error: string | null;
}

/** What the server answers a run with: the result the report draws, and the lake input behind it (#3523). */
export interface IndicatorReliabilityRun extends IndicatorReliabilityResponse {
  /** The lake input behind this result: identity, admitted snapshot, coverage, warmup. */
  data_source: ReliabilityDataSource;
}

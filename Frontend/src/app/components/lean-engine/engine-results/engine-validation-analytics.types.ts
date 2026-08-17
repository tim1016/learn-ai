export interface PerformanceHorizon {
  key: '2w' | '1m' | '3m' | '6m' | '1y' | '2y';
  label: string;
  start_ms_utc: number;
  end_ms_utc: number;
  has_full_coverage: boolean;
  net_return: number | null;
  trade_count: number;
  win_rate: number | null;
  profit_factor: number | null;
}

export interface TimingCell {
  weekday: number;
  weekday_label: string;
  hour_et: number;
  trade_count: number;
  win_rate: number;
  average_return: number;
}

export interface SeasonalityMonth {
  month: number;
  month_label: string;
  observation_count: number;
  median_compounded_return: number | null;
}

export interface RollingTradePoint {
  trade_number: number;
  end_ms_utc: number;
  window_size: number;
  average_return: number;
  win_rate: number;
}

export interface SharpePnlDivergencePoint {
  timestamp_ms_utc: number;
  cumulative_pnl: number;
  rolling_sharpe: number | null;
  is_divergence: boolean;
  is_trend_eligible: boolean;
}

export interface SharpePnlDivergence {
  error: string | null;
  return_window_sessions: number;
  trend_window_sessions: number;
  annualization_sessions: number;
  minimum_observation_count: number;
  daily_observation_count: number;
  eligible_observation_count: number;
  divergence_observation_count: number;
  divergence_ratio: number | null;
  longest_divergence_streak: number;
  latest_rolling_sharpe: number | null;
  latest_cumulative_pnl: number | null;
  currently_diverging: boolean | null;
  points: SharpePnlDivergencePoint[];
}

export interface EngineValidationAnalytics {
  horizons: PerformanceHorizon[];
  timing_cells: TimingCell[];
  seasonality: SeasonalityMonth[];
  rolling_trade_stability: RollingTradePoint[];
  sharpe_pnl_divergence: SharpePnlDivergence | null;
}

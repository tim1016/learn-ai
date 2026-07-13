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

export interface EngineValidationAnalytics {
  horizons: PerformanceHorizon[];
  timing_cells: TimingCell[];
  seasonality: SeasonalityMonth[];
  rolling_trade_stability: RollingTradePoint[];
}


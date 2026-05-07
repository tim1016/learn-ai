/**
 * TypeScript mirrors of the Monte Carlo DTOs from
 * `PythonDataService/app/research/monte_carlo/result.py`.
 *
 * Same wire-format invariants as `walk-forward.types.ts`: every
 * timestamp is `int64 ms UTC`; all metric values come from the server.
 * Aggregate quantile dicts use string keys (`p5`, `p50`, `p95`) and
 * numeric values — Angular renders, never computes.
 */

export type MonteCarloMethod = 'reshuffle' | 'resample';
export type MonteCarloStatus = 'completed' | 'failed';

export interface EquityBandPoint {
  trade_index: number;
  p5: number;
  p50: number;
  p95: number;
}

export interface BreachProbability {
  threshold: number;
  probability: number;
}

export interface MonteCarloConfig {
  monte_carlo_id: string;
  parent_run_id: string;
  parent_trade_log_hash: string;
  method: MonteCarloMethod;
  simulation_count: number;
  projection_trade_count: number;
  initial_equity: number;
  random_seed: number;
  breach_thresholds: number[];
  created_at_ms: number;
}

export interface MonteCarloResult {
  monte_carlo_id: string;
  parent_run_id: string;
  method: MonteCarloMethod;
  simulation_count: number;
  realised_trade_count: number;
  equity_bands: EquityBandPoint[];
  drawdown_quantiles: Record<string, number>;
  terminal_pnl_quantiles: Record<string, number>;
  max_losing_streak_quantiles: Record<string, number>;
  breach_probabilities: BreachProbability[];
  warnings: string[];
  created_at_ms: number;
  completed_at_ms: number | null;
  status: MonteCarloStatus;
  failure_reason: string | null;
}

export interface MonteCarloResponse {
  config: MonteCarloConfig;
  result: MonteCarloResult;
}

export interface MonteCarloListResponse {
  monte_carlos: MonteCarloConfig[];
}

/**
 * Request payload for `POST /api/research/strategy-runs/monte-carlo`.
 * `breach_thresholds` are drawdown fractions in [0, 1].
 */
export interface MonteCarloRequest {
  parent_run_id: string;
  method: MonteCarloMethod;
  simulation_count?: number;
  projection_trade_count?: number;
  random_seed?: number;
  breach_thresholds?: number[];
}

export interface MonteCarloListFilters {
  parent_run_id?: string;
  method?: MonteCarloMethod;
  since_ms?: number;
  limit?: number;
}

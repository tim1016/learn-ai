/**
 * TypeScript mirrors of the null-baseline DTOs from
 * `PythonDataService/app/research/baselines/result.py`.
 *
 * Same wire-format invariants as the other research types: every
 * timestamp is `int64 ms UTC`; all metric values come from the server.
 */

import type { RunMetrics } from './strategy-runs.types';

export type BaselineMethod = 'buy_and_hold' | 'random_ema_windows';
export type BaselineStatus = 'completed' | 'failed';
export type BaselineRunStatus = 'completed' | 'failed';

export interface BaselineRunRecord {
  baseline_run_id: string;
  method: BaselineMethod;
  parameters: Record<string, unknown>;
  test_metrics: RunMetrics;
  test_trade_count: number;
  status: BaselineRunStatus;
  failure_reason: string | null;
}

export interface NullDistribution {
  metric_name: string;
  parent_value: number | null;
  null_values: number[];
  empirical_percentile: number | null;
  empirical_p_value: number | null;
}

export interface BaselineConfig {
  baseline_id: string;
  parent_run_id: string;
  parent_trade_log_hash: string;
  method: BaselineMethod;
  sample_count: number;
  random_seed: number;
  method_params: Record<string, unknown>;
  target_metrics: string[];
  created_at_ms: number;
}

export interface BaselineResult {
  baseline_id: string;
  parent_run_id: string;
  method: BaselineMethod;
  sample_count: number;
  baselines: BaselineRunRecord[];
  null_distributions: NullDistribution[];
  warnings: string[];
  created_at_ms: number;
  completed_at_ms: number | null;
  status: BaselineStatus;
  failure_reason: string | null;
}

export interface BaselineResponse {
  config: BaselineConfig;
  result: BaselineResult;
}

export interface BaselineListResponse {
  baselines: BaselineConfig[];
}

export interface BaselineRequest {
  parent_run_id: string;
  method: BaselineMethod;
  sample_count?: number;
  random_seed?: number;
  fast_range?: [number, number];
  slow_range?: [number, number];
}

export interface BaselineListFilters {
  parent_run_id?: string;
  method?: BaselineMethod;
  since_ms?: number;
  limit?: number;
}

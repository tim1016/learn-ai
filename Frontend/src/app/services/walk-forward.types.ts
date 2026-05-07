/**
 * TypeScript mirrors of the walk-forward DTOs from
 * `PythonDataService/app/research/walk_forward/result.py`.
 *
 * Same wire-format invariants as `strategy-runs.types.ts`: every
 * timestamp is `int64 ms UTC` (typed as `number`, never a Date or
 * ISO string); all metric values come from the server.
 */

import type { EquityCurvePoint, RunMetrics } from './strategy-runs.types';

export type WalkForwardStatus = 'completed' | 'failed';
export type FoldStatus = 'completed' | 'failed';

export type SplitPolicyKind = 'chronological' | 'rolling' | 'anchored';

/**
 * Wire-shape for a split policy. ``kind`` discriminates; the rest of
 * the fields are policy-specific. Server uses Pydantic's
 * ``ConfigDict(extra='allow')`` so the discriminator is enforced but
 * additional fields pass through. Modelled as an indexed interface
 * for the same flexibility (untyped policy-specific fields:
 * chronological → train_pct; rolling → train_days/test_days/step_days;
 * anchored → initial_train_days/test_days/step_days).
 */
export interface SplitPolicySpec {
  kind: SplitPolicyKind;
  [field: string]: unknown;
}

export interface FoldResult {
  fold_index: number;
  train_start_ms: number;
  train_end_ms: number;
  test_start_ms: number;
  test_end_ms: number;
  test_run_id: string;
  test_metrics: RunMetrics;
  test_trade_count: number;
  status: FoldStatus;
  failure_reason: string | null;
  selected_parameters: Record<string, unknown>;
}

export interface WalkForwardConfig {
  walk_forward_id: string;
  parent_run_id: string | null;
  strategy_spec_hash: string;
  strategy_spec_json: Record<string, unknown>;
  symbol: string;
  resolution_minutes: number;
  start_ms: number;
  end_ms: number;
  initial_cash: number;
  fill_mode: string;
  commission_per_order: number;
  slippage_per_share: number;
  random_seed: number;
  split_policy: SplitPolicySpec;
  created_at_ms: number;
}

export interface WalkForwardResult {
  walk_forward_id: string;
  parent_run_id: string | null;
  strategy_spec_hash: string;
  split_policy: SplitPolicySpec;
  folds: FoldResult[];
  combined_oos_equity_curve: EquityCurvePoint[];
  mean_oos_sharpe: number | null;
  median_oos_sharpe: number | null;
  pct_profitable_folds: number | null;
  oos_retention: number | null;
  alpha_decay: number | null;
  warnings: string[];
  created_at_ms: number;
  completed_at_ms: number | null;
  status: WalkForwardStatus;
  failure_reason: string | null;
}

export interface WalkForwardResponse {
  config: WalkForwardConfig;
  result: WalkForwardResult;
}

export interface WalkForwardListResponse {
  walk_forwards: WalkForwardConfig[];
}

/**
 * Request payload for `POST /api/research/strategy-runs/walk-forward`.
 * Spec is the full StrategySpec (typed `unknown` — same call as
 * `StrategyRunRequest`, no spec-schema fork between languages).
 */
export interface WalkForwardRequest {
  spec: unknown;
  start_date: string; // YYYY-MM-DD
  end_date: string;
  split_policy: SplitPolicySpec;
  initial_cash?: number;
  fill_mode?: string;
  commission_per_order?: number;
  slippage_per_share?: number;
  random_seed?: number;
  parent_run_id?: string | null;
}

export interface WalkForwardListFilters {
  parent_run_id?: string;
  spec_hash?: string;
  since_ms?: number;
  limit?: number;
}

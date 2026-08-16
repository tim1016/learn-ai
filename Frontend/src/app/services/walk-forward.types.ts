/**
 * TypeScript mirrors of the walk-forward DTOs from
 * `PythonDataService/app/research/walk_forward/result.py`.
 *
 * Same wire-format invariants as `strategy-runs.types.ts`: every
 * timestamp is `int64 ms UTC` (typed as `number`, never a Date or
 * ISO string); all metric values come from the server.
 */

import type {
  EquityCurvePoint,
  RunMetrics,
  StrategyRunResponse,
} from './strategy-runs.types';

export type WalkForwardStatus = 'completed' | 'failed';
export type FoldStatus = 'completed' | 'failed';

export type SplitPolicyKind = 'chronological' | 'rolling' | 'anchored';

export type SplitPolicySpec =
  | { kind: 'chronological'; train_pct: number }
  | { kind: 'rolling'; train_days: number; test_days: number; step_days: number }
  | { kind: 'anchored'; initial_train_days: number; test_days: number; step_days: number };

export interface FoldResult {
  fold_index: number;
  train_start_ms: number;
  train_end_ms: number;
  test_start_ms: number;
  test_end_ms: number;
  test_run_id: string | null;
  test_metrics: RunMetrics;
  test_trade_count: number;
  status: FoldStatus;
  failure_reason: string | null;
  selected_parameters: Record<string, number>;
  training_candidates: TrainingCandidateResult[];
  selected_train_sharpe: number | null;
  oos_retention: number | null;
}

export interface ParameterCandidateConfig {
  parameters: Record<string, number>;
  strategy_spec_hash: string;
  strategy_spec_json: Record<string, unknown>;
}

export interface ParameterSearchConfig {
  objective: 'sharpe_ratio';
  min_trades: number;
  candidates: ParameterCandidateConfig[];
}

export interface TrainingCandidateResult {
  parameters: Record<string, number>;
  train_run_id: string;
  train_metrics: RunMetrics;
  receipt_persisted: boolean;
  eligible: boolean;
  ineligibility_reason: string | null;
}

export interface SelectionFailureResult {
  fold_index: number;
  train_start_ms: number;
  train_end_ms: number;
  test_start_ms: number;
  test_end_ms: number;
  training_candidates: TrainingCandidateResult[];
  failure_reason: string;
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
  parameter_search: ParameterSearchConfig | null;
  fold_position_policy: 'flat_at_test_boundaries';
  protocol_id: string | null;
  protocol_version: string | null;
  created_at_ms: number;
}

export interface WalkForwardResult {
  walk_forward_id: string;
  parent_run_id: string | null;
  strategy_spec_hash: string;
  split_policy: SplitPolicySpec;
  folds: FoldResult[];
  selection_failures: SelectionFailureResult[];
  combined_oos_equity_curve: EquityCurvePoint[];
  mean_oos_sharpe: number | null;
  median_oos_sharpe: number | null;
  pct_profitable_folds: number | null;
  oos_retention: number | null;
  oos_retention_basis:
    | 'mean_fold_test_to_selected_train'
    | 'mean_oos_to_parent'
    | null;
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
  start_ms: number;
  end_ms: number;
  split_policy: SplitPolicySpec;
  initial_cash?: number;
  fill_mode?: string;
  commission_per_order?: number;
  slippage_per_share?: number;
  random_seed?: number;
  parent_run_id?: string | null;
}

interface WalkForwardBaseFilters {
  parent_run_id?: string;
  spec_hash?: string;
  since_ms?: number;
  limit?: number;
}

type ProtocolIdentityFilter =
  | { protocol_id: string; protocol_version: string }
  | { protocol_id?: never; protocol_version?: never };

export type WalkForwardListFilters = WalkForwardBaseFilters & ProtocolIdentityFilter;

export interface SpyEmaPipelineResponse {
  control: StrategyRunResponse;
  walk_forward: WalkForwardResponse;
}

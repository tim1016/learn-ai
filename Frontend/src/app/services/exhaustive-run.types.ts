import type { RunMetrics } from './strategy-runs.types';

export type ExhaustiveRunStatus = 'completed' | 'failed';

export interface FoldCandidateSelection {
  fold_index: number;
  fold_rank: number;
  parameters: Record<string, number>;
  strategy_spec_hash: string;
  train_run_id: string;
  train_metrics: RunMetrics;
  selection_score: number;
}

export interface CandidateFoldEvidence {
  fold_index: number;
  train_start_ms: number;
  train_end_ms: number;
  test_start_ms: number;
  test_end_ms: number;
  train_run_id: string;
  test_run_id: string | null;
  train_metrics: RunMetrics;
  test_metrics: RunMetrics;
  retention: number | null;
  status: ExhaustiveRunStatus;
  failure_reason: string | null;
}

export interface CandidateForwardStability {
  walk_forward_id: string;
  folds: CandidateFoldEvidence[];
  pct_profitable_folds: number | null;
  mean_oos_sharpe: number | null;
  median_oos_sharpe: number | null;
  alpha_decay: number | null;
  mean_fold_retention: number | null;
}

export interface TradeRecencySummary {
  recent_window_start_ms: number;
  recent_window_end_ms: number;
  recent_trade_count: number;
  prior_trade_count: number;
  total_trade_count: number;
  recent_trade_share: number | null;
  recent_vs_prior_rate_ratio: number | null;
  last_trade_exit_ms: number | null;
}

export interface ExhaustiveCandidateResult {
  parameters: Record<string, number>;
  strategy_spec_hash: string;
  appearance_count: number;
  appearance_fold_indices: number[];
  best_fold_rank: number;
  latest_selected_fold_index: number;
  mean_selection_score: number;
  status: ExhaustiveRunStatus;
  full_period_run_id: string | null;
  full_period_metrics: RunMetrics | null;
  trade_recency: TradeRecencySummary | null;
  forward_stability: CandidateForwardStability | null;
  failure_reason: string | null;
}

export interface ExhaustiveRunConfig {
  exhaustive_run_id: string;
  source_walk_forward_id: string;
  protocol_id: 'spy-ema-exhaustive-run';
  protocol_version: '1.0';
  source_protocol_id: string;
  source_protocol_version: string;
  start_ms: number;
  end_ms: number;
  recent_window_start_ms: number;
  max_candidates_per_fold: 5;
  ranking_method: 'equal_weight_train_sharpe_return_percentile';
  initial_cash: number;
  fill_mode: string;
  commission_per_order: number;
  slippage_per_share: number;
  random_seed: number;
  data_root_revision: string;
  created_at_ms: number;
}

export interface ExhaustiveRunResult {
  exhaustive_run_id: string;
  source_walk_forward_id: string;
  selections: FoldCandidateSelection[];
  candidates: ExhaustiveCandidateResult[];
  warnings: string[];
  status: ExhaustiveRunStatus;
  failure_reason: string | null;
  created_at_ms: number;
  completed_at_ms: number;
}

export interface ExhaustiveRunResponse {
  config: ExhaustiveRunConfig;
  result: ExhaustiveRunResult;
}

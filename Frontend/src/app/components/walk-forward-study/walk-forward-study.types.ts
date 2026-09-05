/**
 * Wire shapes for Walk-Forward Studies (PRD #1925), mirroring
 * PythonDataService/app/schemas/walk_forward_study.py. Every temporal value
 * is int64 ms UTC; fold boundaries are ET midnight instants with exclusive ends.
 */

import type { GridSearchSpecRequest, GridSearchStatus, RankingMeasure } from '../grid-search/grid-search.types';

export type WalkForwardStudyStatus = GridSearchStatus;
export type FoldStatus = 'pending' | 'running' | 'completed' | 'failed';
export type VerdictLabel = 'still worked' | 'got worse' | 'stopped working' | 'too few trades' | 'could not be judged';

export interface WalkForwardStudySpecRequest extends GridSearchSpecRequest {
  training_months: number;
  test_months: number;
}

export interface FoldPlan {
  fold_index: number;
  train_start_ms: number;
  train_end_ms: number;
  test_start_ms: number;
  test_end_ms: number;
}

export interface WalkForwardStudyPreflight {
  strategy_key: string;
  symbol: string;
  combinations: number;
  fold_count: number;
  total_backtests: number;
  backtest_limit: number;
  estimated_seconds: number;
  required_samples: number;
  run_up_sessions: number;
  folds: FoldPlan[];
}

export interface Fold extends FoldPlan {
  status: FoldStatus;
  train_search_id: string | null;
  test_search_id: string | null;
  winner_params_hash: string | null;
  winner_params: Record<string, number> | null;
  train_sharpe: number | null;
  test_sharpe: number | null;
  test_trades: number;
  retention: number | null;
  failure_reason: string | null;
}

export interface Verdict {
  label: VerdictLabel;
  reason: string;
  successful_folds: number;
  defined_folds: number;
  study_retention: number | null;
  median_test_sharpe: number | null;
  oos_trade_count: number;
  based_on: string;
}

export interface WalkForwardStudySummary {
  id: string;
  strategy_key: string;
  symbol: string;
  status: WalkForwardStudyStatus;
  job_id: string | null;
  created_at_ms: number;
  finished_at_ms: number | null;
  window_start_ms: number;
  window_end_ms: number;
  training_months: number;
  test_months: number;
  measure: RankingMeasure;
  min_trades: number;
  fold_count: number;
  completed_folds: number;
  failed_folds: number;
  expected_backtests: number;
  completed_backtests: number;
  verdict: Verdict | null;
  winner_changes: number;
  incomplete: boolean;
  uncommitted_changes: boolean;
  failure_reason: string | null;
}

export interface WalkForwardStudyDetail extends WalkForwardStudySummary {
  request: WalkForwardStudySpecRequest;
  receipt: Record<string, unknown>;
  folds: Fold[];
  resumable: boolean;
  resume_refusal: string | null;
}

export interface WalkForwardStudyHistoryFilters {
  strategy_key?: string;
  symbol?: string;
  status?: WalkForwardStudyStatus;
  job_id?: string;
}

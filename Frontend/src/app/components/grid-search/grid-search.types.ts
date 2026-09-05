/**
 * Wire shapes for Grid Search (PRD #1926), mirroring
 * PythonDataService/app/schemas/grid_search.py. Every temporal value is
 * int64 ms UTC; date-anchored boundaries are ET midnight instants.
 */

import type { ParamRange } from '../../shared/param-range/param-range';

export type RankingMeasure = 'sharpe_ratio' | 'total_return_pct' | 'net_profit';
export const RANKING_MEASURES: readonly RankingMeasure[] = ['sharpe_ratio', 'total_return_pct', 'net_profit'];

export type GridSearchStatus = 'queued' | 'running' | 'completed' | 'failed' | 'cancelled' | 'interrupted';

export interface GridSearchSpecRequest {
  strategy_key: string;
  symbol: string;
  param_ranges: Record<string, ParamRange>;
  start_ms: number;
  end_ms: number;
  resolution: 'minute' | 'daily';
  fill_mode: string;
  commission_per_order: number;
  slippage_per_share: number;
  initial_cash: number;
  measure: RankingMeasure;
  min_trades: number;
}

export interface RunUpPlan {
  data_start_ms: number;
  evaluation_start_ms: number;
  evaluation_end_ms: number;
  required_samples: number;
  bar_span_ms: number;
  run_up_sessions: number;
  carved_from_range: boolean;
}

export interface GridSearchPreflight {
  strategy_key: string;
  symbol: string;
  combinations: number;
  total_backtests: number;
  backtest_limit: number;
  estimated_seconds: number;
  run_up: RunUpPlan;
  expected_sessions: number;
}

export interface GridSearchRefusal {
  code: string;
  message: string;
}

export interface SearchOwner {
  kind: 'user' | 'walk_forward';
  owner_id: string | null;
  fold_index: number | null;
  phase: string | null;
}

export interface GridSearchSummary {
  id: string;
  owner: SearchOwner;
  strategy_key: string;
  symbol: string;
  status: GridSearchStatus;
  job_id: string | null;
  created_at_ms: number;
  finished_at_ms: number | null;
  window_start_ms: number;
  window_end_ms: number;
  measure: RankingMeasure;
  min_trades: number;
  expected_cells: number;
  completed_cells: number;
  failed_cells: number;
  leader_params_hash: string | null;
  leader_params: Record<string, number> | null;
  incomplete: boolean;
  uncommitted_changes: boolean;
  failure_reason: string | null;
}

export interface GridSearchDetail extends GridSearchSummary {
  request: GridSearchSpecRequest;
  receipt: Record<string, unknown>;
  resumable: boolean;
  resume_refusal: string | null;
}

export interface GridSearchCell {
  params_hash: string;
  params: Record<string, number>;
  status: 'completed' | 'failed';
  attempt: number;
  total_trades: number;
  net_profit: number | null;
  total_return_pct: number | null;
  sharpe_ratio: number | null;
  max_drawdown_pct: number | null;
  win_rate: number | null;
  bars_consumed: number | null;
  error: string | null;
  exploratory: boolean;
  completed_at_ms: number;
  is_leader: boolean;
  eligible: boolean;
}

export type CellSortColumn = 'sharpe_ratio' | 'total_return_pct' | 'net_profit' | 'total_trades' | 'max_drawdown_pct' | 'win_rate' | 'params_hash';

export interface CellPageQuery {
  sort_by: CellSortColumn;
  direction: 'asc' | 'desc';
  page: number;
  page_size: number;
}

export interface GridSearchCellPage extends CellPageQuery {
  total: number;
  cells: GridSearchCell[];
}

export interface GridSearchHistoryFilters {
  strategy_key?: string;
  symbol?: string;
  status?: GridSearchStatus;
  job_id?: string;
}

export const TERMINAL_STATUSES: readonly GridSearchStatus[] = ['completed', 'failed', 'cancelled', 'interrupted'];

export function isTerminal(status: GridSearchStatus): boolean {
  return TERMINAL_STATUSES.includes(status);
}

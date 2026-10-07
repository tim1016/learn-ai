/**
 * Wire shapes for Grid Search (PRD #1926), mirroring
 * PythonDataService/app/schemas/grid_search.py. Every temporal value is
 * int64 ms UTC; date-anchored boundaries are ET midnight instants.
 */

import type { FillModeName } from '../../models/fill-mode';
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
  fill_mode: FillModeName;
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
  /** The cell kept its daily equity; a failed cell never does. */
  has_curve: boolean;
  is_leader: boolean;
  eligible: boolean;
}

export type CellSortColumn = 'sharpe_ratio' | 'total_return_pct' | 'net_profit' | 'total_trades' | 'max_drawdown_pct' | 'win_rate' | 'params_hash';

export interface CellPageQuery {
  sort_by: CellSortColumn;
  direction: 'asc' | 'desc';
  page: number;
  page_size: number;
  /** Put the leader's cell first regardless of the sort (a study-owned sweep's evidence row). */
  pin_leader?: boolean;
}

export interface GridSearchCellPage extends CellPageQuery {
  total: number;
  /** Completed cells of the whole search that kept no daily equity. */
  without_curve: number;
  cells: GridSearchCell[];
}

/** One cell's daily curve: its values from `first_session` on along its read's `sessions_ms`, null at a session it did not score. */
export interface GridSearchCellCurve {
  params_hash: string;
  first_session: number;
  cumulative_return: (number | null)[];
  /** Null as a whole when the cell's peak equity never rose above 0. */
  drawdown: (number | null)[] | null;
  deepest_drawdown: number | null;
}

export interface GridSearchCellCurves {
  /** Every session close any returned curve scored, in order. */
  sessions_ms: number[];
  curves: GridSearchCellCurve[];
  /** The cells asked for that kept no daily equity. */
  not_recorded: string[];
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

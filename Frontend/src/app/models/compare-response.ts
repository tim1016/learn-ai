/**
 * PR B (2026-05-19) Phase 4 — wire shape for ``GET /api/runs/compare``.
 *
 * Mirrors spec § 6.5 verbatim (snake_case). Used by the
 * ``RunsCompareService`` HTTP client and the ``RunsCompareComponent``
 * template; matches the .NET ``CompareResponse`` record returned by the
 * Backend ``CompareController`` minimal-API endpoint.
 */

import type { DataPolicy } from './data-policy';

export interface CompareResponse {
  left: RunSummary;
  right: RunSummary;
  compatible: boolean;
  mismatches: string[];
  informational_differences: string[];
  summary_deltas: SummaryDeltas;
  trade_diff: TradeDiff;
  first_divergence: TradeDivergence | null;
  state_trace_available: boolean;
  raw_run_links: RawRunLinks;
}

export interface RunSummary {
  id: number;
  engine: 'PYTHON' | 'LEAN';
  data_policy: DataPolicy | null;
  summary: RunSummaryStats;
  starting_cash: number;
  commission_per_order: string;
  fill_mode: string;
  brokerage_policy: string | null;
  strategy_identity: StrategyIdentity;
}

export interface RunSummaryStats {
  total_trades: number;
  total_pnl: number;
  total_fees: number;
  win_rate: number;
  max_drawdown: number;
  sharpe: number;
}

export interface StrategyIdentity {
  kind: 'python_registry' | 'lean_template' | 'lean_source';
  name: string;
  sha256: string | null;
}

export interface SummaryDeltas {
  total_trades: SummaryDelta<number>;
  total_pnl: SummaryDelta<number>;
  total_fees: SummaryDelta<number>;
  win_rate: SummaryDelta<number>;
  max_drawdown: SummaryDelta<number>;
  sharpe: SummaryDelta<number>;
}

export interface SummaryDelta<T> {
  left: T;
  right: T;
  delta: T;
}

export interface TradeDiff {
  matched_pairs: MatchedTradePair[];
  python_only: UnmatchedTrade[];
  lean_only: UnmatchedTrade[];
  first_divergence: TradeDivergence | null;
}

export interface MatchedTradePair {
  trade_number: number;
  entry_ts_delta_ms: number;
  exit_ts_delta_ms: number;
  entry_price_delta: string;
  exit_price_delta: string;
  qty_delta: string;
  pnl_delta: string;
  category: string;
}

export interface UnmatchedTrade {
  trade_number: number;
  entry_ms_utc: number;
  exit_ms_utc: number;
  entry_price: string;
  exit_price: string;
  quantity: string;
  pnl: string;
}

export interface TradeDivergence {
  trade_index: number;
  what: string;
  category: string;
  left_value: string;
  right_value: string;
}

export interface RawRunLinks {
  left: RawRunSide;
  right: RawRunSide;
}

export interface RawRunSide {
  manifest_path: string | null;
  log_path: string | null;
  staged_zip_sha256: Record<string, string> | null;
}

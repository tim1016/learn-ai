/**
 * TypeScript mirrors of the run-ledger DTOs from
 * `PythonDataService/app/research/runs/{ledger,result}.py`.
 *
 * Wire-format invariants enforced here (validate, don't recompute):
 *   - Every timestamp is `int64 ms UTC` — `number` in TS, never a Date or
 *     ISO string. UI formats locally at the display boundary; nothing
 *     round-trips through this type as a string.
 *   - All metric values come from the server. Angular renders, formats,
 *     sorts. It does not compute.
 *
 * Mirror structure 1:1; if the Python schema bumps `schema_version`, this
 * file is the migration target — don't loosen the types here.
 */

export type RunStatus = 'running' | 'completed' | 'failed';

export type RunResult = 'WIN' | 'LOSS';

export interface RunLedger {
  schema_version: '1.0';
  run_id: string;

  parent_run_id: string | null;
  parent_spec_hash: string | null;

  strategy_spec_id: string;
  strategy_spec_hash: string;
  strategy_spec_json: Record<string, unknown>;

  engine_name: 'learn_ai_event_driven';
  engine_version: string;
  engine_git_commit: string;

  symbol: string;
  resolution_minutes: number;
  start_ms: number;
  end_ms: number;
  initial_cash: number;
  fill_mode: string;
  commission_per_order: number;
  slippage_per_share: number;
  warmup_policy: 'spec_indicator_warmup';
  random_seed: number;

  data_source: 'lean_minute_reader';
  data_snapshot_id: string;

  result_hash: string | null;
  trade_log_hash: string | null;
  metrics_hash: string | null;

  created_at_ms: number;
  completed_at_ms: number | null;
  status: RunStatus;
  failure_reason: string | null;
}

export interface EquityCurvePoint {
  timestamp_ms: number;
  equity: number;
}

export interface DrawdownPoint {
  timestamp_ms: number;
  drawdown_pct: number;
}

export interface RunTrade {
  trade_number: number;
  entry_time_ms: number;
  entry_price: number;
  exit_time_ms: number;
  exit_price: number;
  indicators_at_entry: Record<string, number>;
  pnl_pts: number;
  pnl_pct: number;
  result: RunResult;
  signal_reason: string;
  bars_held: number;
}

export interface RunMetrics {
  total_trades: number;
  winning_trades: number;
  losing_trades: number;
  win_rate: number | null;
  total_return_pct: number;
  max_drawdown_pct: number | null;
  sharpe_ratio: number | null;
  sortino_ratio: number | null;
  profit_factor: number | null;
  expectancy_pct: number | null;
  payoff_ratio: number | null;
  exposure_pct: number | null;
  avg_trade_bars: number | null;
}

export interface BacktestRunResult {
  run_id: string;
  initial_cash: number;
  final_equity: number;
  equity_curve: EquityCurvePoint[];
  drawdown_curve: DrawdownPoint[];
  trades: RunTrade[];
  metrics: RunMetrics;
  log_lines: string[];
  warnings: string[];
}

export interface StrategyRunResponse {
  ledger: RunLedger;
  result: BacktestRunResult;
}

export interface StrategyRunListResponse {
  runs: RunLedger[];
}

/**
 * Request payload for `POST /api/research/strategy-runs`. Mirrors the
 * Pydantic `StrategyRunRequest` model. The embedded `spec` is the full
 * `StrategySpec` (validated by the Python router via Pydantic) — typed
 * here as `unknown` so we don't fork the spec schema across languages.
 * Callers either pass a fixture spec verbatim or build one through a
 * future spec-editor component.
 */
export interface StrategyRunRequest {
  spec: unknown;
  start_date: string; // YYYY-MM-DD
  end_date: string;
  initial_cash?: number;
  fill_mode?: string;
  commission_per_order?: number;
  slippage_per_share?: number;
  random_seed?: number;
  strategy_spec_id?: string;
  parent_run_id?: string | null;
  parent_spec_hash?: string | null;
}

/** Filters mirror the FastAPI listing endpoint's query params. */
export interface StrategyRunListFilters {
  spec_hash?: string;
  symbol?: string;
  status?: RunStatus;
  parent_run_id?: string;
  parent_spec_hash?: string;
  since_ms?: number;
  limit?: number;
}

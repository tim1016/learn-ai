/**
 * Hand-mirrored REST shapes for the bot-trade chart card.
 *
 * Mirrors the Pydantic models on the Python service:
 *   - app/broker/ibkr/models.py :: IbkrMinuteBar, IbkrBarsSnapshot
 *   - app/services/live_log_failures.py :: FailureRow
 *   - app/engine/live/artifacts.py :: TRADE_COLUMNS, EXECUTION_COLUMNS
 *
 * Kept local rather than regenerated through broker.types.ts so a future
 * OpenAPI refresh doesn't churn unrelated types in this PR.
 */

/** One closed 1-min OHLCV bar from the IBKR aggregator. Decimals come
 * across as strings (Pydantic Decimal serialization). */
export interface IbkrMinuteBar {
  symbol: string;
  start_ms: number;
  end_ms: number;
  open: string;
  high: string;
  low: string;
  close: string;
  volume: number;
  fetched_at_ms: number;
}

export type BarsSubscriptionStatus =
  | 'idle'
  | 'subscribing'
  | 'streaming'
  | 'errored';

export interface IbkrBarsSnapshot {
  symbol: string;
  status: BarsSubscriptionStatus;
  last_error: string | null;
  last_bar_ms: number | null;
  bars: IbkrMinuteBar[];
}

/** One row from trades.parquet — entry/exit pair with realized PnL points. */
export interface TradeRow {
  entry_time_ms: number;
  exit_time_ms: number;
  entry_price: number;
  exit_price: number;
  pnl_points: number;
}

/** One row from executions.parquet — single broker fill event. */
export interface ExecutionRow {
  ts_ms: number;
  exec_id: string;
  perm_id: number;
  client_order_id: string;
  account_id: string;
  symbol: string;
  fill_quantity: number;
  fill_price: number;
  fee: number;
  execution_source: string;
  fill_model: string;
  source_bar_close_ms: number | null;
}

/** One parsed ERROR/CRITICAL block from live.log. ``raw_ts`` is the
 * literal log string (host-local TZ); ``ts_ms`` parses it as if UTC and
 * is only valid for sequencing. */
export interface FailureRow {
  ts_ms: number;
  raw_ts: string;
  level: 'ERROR' | 'CRITICAL';
  logger: string;
  message: string;
  traceback: string | null;
}

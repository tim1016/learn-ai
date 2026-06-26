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

/** Slice 6 — one date the operator can pick from the chart's date selector.
 * ``has_bars=false`` means the instance ran that day but the bars pre-date
 * the persistence layer; the chart shows trade markers + a "bars
 * unavailable" badge. */
export interface ActiveDateEntry {
  date: string;
  run_count: number;
  has_bars: boolean;
}

/** Slice 5 — one run's contribution to the aggregated chart payload. */
export interface ChartSnapshotRun {
  run_id: string;
  started_at_ms: number | null;
  ended_at_ms: number | null;
  is_current: boolean;
  color_index: number;
  trades: TradeRow[];
  executions: ExecutionRow[];
}

/** Slice 5 — aggregated chart payload for one (instance, date, resolution). */
export interface ChartSnapshotResponse {
  date: string;
  symbol: string;
  resolution: '1m' | '5s';
  has_bars: boolean;
  now_ms: number;
  bars: IbkrMinuteBar[];
  runs: ChartSnapshotRun[];
}

export interface ActivityEvidenceRef {
  source: string;
  seq: number;
  ts_ms: number;
  request_call: string;
  response_callback: string | null;
}

export interface ActivityFillMarker {
  id: string;
  row_seq: number;
  order_key: string;
  symbol: string;
  side: 'BUY' | 'SELL';
  quantity: number;
  price: number;
  exec_ts_ms: number;
  position_effect: string;
  replay_count: number;
  evidence: ActivityEvidenceRef[];
}

export interface ActivityPositionAnnotation {
  id: string;
  ts_ms: number;
  symbol: string;
  label: string;
  net_position: number;
  uncertain: boolean;
  reason: string | null;
}

export interface ActivityOrderOverlay {
  id: string;
  order_key: string;
  symbol: string;
  side: 'BUY' | 'SELL';
  quantity: number;
  price: number;
  status: string;
  ts_ms: number;
}

export interface ActivityOrderRow {
  order_key: string;
  symbol: string;
  side: 'BUY' | 'SELL';
  quantity: number;
  order_type: string;
  status: string;
  group: 'active' | 'resolved' | 'engine_pending';
  submitted_ts_ms: number;
  last_update_ts_ms: number;
  filled_quantity: number;
  avg_fill_price: number | null;
  position_effect: string | null;
  replay_count: number;
  evidence: ActivityEvidenceRef[];
}

export interface ActivityBrokerEventRow {
  id: string;
  visible_row_id: string;
  ts_ms: number;
  row_type: string;
  display_type: string;
  source: string;
  source_label: string;
  symbol: string | null;
  side: 'BUY' | 'SELL' | null;
  quantity: number | null;
  price: number | null;
  status: string | null;
  summary: string;
  verdict: string;
  replay_count: number;
  fold_key: string | null;
  fold_count: number;
  cluster_key: string | null;
  cluster_label: string | null;
  child_evidence_ids: string[];
  constituent_fill_ids: string[];
  evidence: ActivityEvidenceRef[];
}

export interface ActivityPositionSnapshot {
  symbol: string;
  quantity: number;
  source: 'broker_snapshot' | 'unavailable';
  as_of_ms: number | null;
}

export interface ActivityReconciliationWarning {
  code: string;
  message: string;
  row_ids: string[];
}

export interface LiveInstanceActivityProjection {
  schema_version: number;
  strategy_instance_id: string;
  session_date: string;
  timezone: string;
  symbol: string;
  resolution: '1m' | '5s';
  has_bars: boolean;
  now_ms: number;
  bars: IbkrMinuteBar[];
  fill_markers: ActivityFillMarker[];
  position_annotations: ActivityPositionAnnotation[];
  order_overlays: ActivityOrderOverlay[];
  orders_today: ActivityOrderRow[];
  broker_activity_rows: ActivityBrokerEventRow[];
  position_snapshot: ActivityPositionSnapshot[];
  reconciliation_warnings: ActivityReconciliationWarning[];
  evidence: ActivityEvidenceRef[];
}

/** One parsed ERROR/CRITICAL block from live.log. ``raw_ts`` is the
 * verbatim UTC log string (the engine logger pins ``time.gmtime``);
 * ``ts_ms`` is the same instant as canonical ``int64`` ms UTC. */
export interface FailureRow {
  ts_ms: number;
  raw_ts: string;
  level: 'ERROR' | 'CRITICAL';
  logger: string;
  message: string;
  traceback: string | null;
}

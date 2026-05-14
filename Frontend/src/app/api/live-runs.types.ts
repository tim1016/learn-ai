export type RunState =
  | 'idle'
  | 'waiting_for_bars'
  | 'warming_up'
  | 'running'
  | 'stale'
  | 'halted'
  | 'poisoned'
  | 'complete'
  | 'stopped'
  | 'unknown';

export type ExitReason =
  | 'normal'
  | 'force_flat_complete'
  | 'keyboard_interrupt'
  | 'signal'
  | 'max_orders_exceeded'
  | 'fatal_halt'
  | 'recovery_flatten'
  | 'exception';

export interface LiveRunSummary {
  run_id: string;
  account_id: string;
  session_start_ms: number;
  created_at_ms: number;
  run_started_at_ms: number | null;
  ended_at_ms: number | null;
  last_activity_ms: number;
  state: RunState;
  decision_count: number;
  execution_count: number;
  halt_flag_set: boolean;
  poisoned_flag_set: boolean;
}

export interface DecisionsSummary {
  row_count: number;
  latest_decision: Record<string, unknown> | null;
}

export interface ExecutionsSummary {
  row_count: number;
  last_fills: Record<string, unknown>[];
}

export interface TradesSummary {
  row_count: number;
  open_position: Record<string, unknown> | null;
}

export interface FlagsSummary {
  halt_flag: Record<string, unknown> | null;
  poisoned_flag: Record<string, unknown> | null;
}

export interface ArtifactFile {
  name: string;
  size_bytes: number | null;
  mtime_ms: number | null;
  row_count: number | null;
}

export interface ArtifactsSummary {
  files: ArtifactFile[];
}

export interface ReconcileSummary {
  latest_receipt_name: string | null;
  latest_receipt_url: string | null;
}

export interface LiveRunStatus {
  run_id: string;
  account_id: string;
  state: RunState;
  last_bar_time_ms: number | null;
  last_bar_age_s: number | null;
  heartbeat_parse_status: 'ok' | 'degraded' | 'no_bars_yet';
  decisions: DecisionsSummary;
  executions: ExecutionsSummary;
  trades: TradesSummary;
  flags: FlagsSummary;
  artifacts: ArtifactsSummary;
  reconcile: ReconcileSummary;
  fetched_at_ms: number;
}

export interface LogLine {
  ts_ms: number | null;
  raw_text: string;
  event_type: 'bar' | 'raw';
  consolidator_emitted: number | null;
  snapshot_set: string | null;
}

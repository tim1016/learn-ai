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

/**
 * Durable operator intent (desired state).
 *
 * Sourced from `artifacts/live_state/<strategy_instance_id>/desired_state.json`
 * via the sibling backend PR `prd-a/ui-1-status-and-controls-api`. The
 * `path_status` discriminates how the desired-state sidecar was resolved:
 *
 * - `ok`        — sidecar read successfully; `state` is authoritative.
 * - `absent`    — no sidecar file yet; engine default is RUNNING.
 * - `corrupt`   — sidecar exists but could not be parsed; controls block.
 * - `unknown_no_ledger_binding` — the run ledger carries no
 *   `strategy_instance_id`, so the sidecar cannot be located at all.
 *   Never guessed — surfaced explicitly.
 */
export type DesiredStateValue = 'RUNNING' | 'PAUSED' | 'STOPPED';

export type DesiredStatePathStatus =
  | 'ok'
  | 'absent'
  | 'corrupt'
  | 'unknown_no_ledger_binding';

export interface DesiredState {
  state: DesiredStateValue | null;
  updated_at_ms: number | null;
  updated_by: string | null;
  reason: string | null;
  version: number | null;
  path_status: DesiredStatePathStatus;
}

/** Operator-issued desired-state transition verbs (UI-3). */
export type DesiredStateAction = 'pause' | 'resume' | 'stop';

export interface DesiredStateWriteRequest {
  action: DesiredStateAction;
  reason?: string;
}

export interface DesiredStateWriteResponse {
  accepted: boolean;
  desired_state: DesiredState;
}

/** Per-run command-channel verbs (UI-4, Resolution 7). */
export type CommandVerb =
  | 'PAUSE'
  | 'RESUME'
  | 'STOP'
  | 'FLATTEN'
  | 'MARK_POISONED'
  | 'RECONCILE';

/**
 * One command-channel entry, derived from the real command files under
 * `artifacts/live_runs/<run_id>/commands/`. A `pending` file becomes
 * `acknowledged` once the matching `command.<seq>.<verb>.ack.json`
 * appears; `failed` when that ack carries an error outcome.
 */
export type CommandStatus = 'queued' | 'acknowledged' | 'failed';

export interface CommandEntry {
  seq: number;
  verb: CommandVerb;
  status: CommandStatus;
  reason: string | null;
  issued_by: string | null;
  queued_at_ms: number | null;
  acked_at_ms: number | null;
  outcome: string | null;
  outcome_detail: string | null;
}

export interface CommandsSummary {
  entries: CommandEntry[];
  poll_interval_ms: number;
}

export interface CommandWriteRequest {
  verb: CommandVerb;
  reason?: string;
}

export interface CommandWriteResponse {
  accepted: boolean;
  command: CommandEntry;
}

export interface LiveRunStatus {
  run_id: string;
  account_id: string;
  state: RunState;
  strategy_instance_id: string | null;
  desired_state: DesiredState;
  bar_source: string | null;
  last_bar_time_ms: number | null;
  last_bar_age_s: number | null;
  heartbeat_parse_status: 'ok' | 'degraded' | 'no_bars_yet';
  decisions: DecisionsSummary;
  executions: ExecutionsSummary;
  trades: TradesSummary;
  flags: FlagsSummary;
  artifacts: ArtifactsSummary;
  reconcile: ReconcileSummary;
  commands: CommandsSummary;
  fetched_at_ms: number;
}

export interface LogLine {
  ts_ms: number | null;
  raw_text: string;
  event_type: 'bar' | 'raw';
  consolidator_emitted: number | null;
  snapshot_set: string | null;
}

export type HydratePolicy = 'require' | 'optional' | 'disabled';

export type HostRunnerProcessState = 'idle' | 'running' | 'exited' | 'stopping';

export interface HostRunnerProcessStatus {
  state: HostRunnerProcessState;
  run_id: string | null;
  pid: number | null;
  started_at_ms: number | null;
  ended_at_ms: number | null;
  exit_code: number | null;
  command: string[];
  log_path: string | null;
  message: string | null;
}

export interface HostRunnerHealth {
  ok: boolean;
  repo_root: string;
  live_runs_root: string;
  fetched_at_ms: number;
  process: HostRunnerProcessStatus;
}

export interface HostRunnerStartRequest {
  readonly: boolean;
  hydrate_policy: HydratePolicy;
  strategy: string;
  max_orders_per_day: number;
  ibkr_host: string;
}

export interface HostRunnerStopRequest {
  force: boolean;
}

export interface HostRunnerActionResponse {
  accepted: boolean;
  process: HostRunnerProcessStatus;
}

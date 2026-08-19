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
 * from historical run artifacts. The `path_status` discriminates how the
 * desired-state sidecar was resolved:
 *
 * - `ok`        — sidecar read successfully; `state` is authoritative.
 * - `absent`    — no sidecar file yet; engine default is RUNNING.
 * - `corrupt`   — sidecar exists but could not be parsed.
 * - `unknown_no_ledger_binding` — the run ledger carries no
 *   `strategy_instance_id`, so the sidecar cannot be located at all.
 *   Never guessed — surfaced explicitly.
 */
// Canonical read-only artifact primitives live in `live-runs-controls.types.ts`.
import type {
  CommandVerb,
  DesiredStateView,
} from './live-runs-controls.types';

export type {
  CommandVerb,
  DesiredStatePathStatus,
  DesiredStateValue,
} from './live-runs-controls.types';

/**
 * Resolved durable-intent view consumed by the UI. Structurally identical to
 * `DesiredStateView` from the control-plane contract; aliased so the UI layer
 * has a stable local name.
 */
export type DesiredState = DesiredStateView;

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

export type MutationRungReceiptCode =
  | 'mutation.next_blocking_rung'
  | 'mutation.scoped_all_clear'
  | 'mutation.observational_warning';

// Shared notice vocabulary — mirrors PythonDataService/app/operator/notices/schema.py.
// Declared here (the dependency leaf) and re-exported from live-instances.types.ts;
// OperatorNotice and MutationRungReceipt consume the same unions.
export type OperatorNoticeTier = 'info' | 'warning' | 'critical';

export type OperatorNoticeActionability =
  | 'actuatable'
  | 'routed'
  | 'self_resolving'
  | 'no_remedy';

export type OperatorNoticeRemedyStatus = 'inherent' | 'unbuilt';

export type OperatorNoticeActionKind =
  | 'none'
  | 'open_runbook'
  | 'focus_cockpit_action'
  | 'renew_control_plane_lease'
  | 'external_manual_check'
  | 'redeploy';

export interface OperatorNoticeAction {
  kind: OperatorNoticeActionKind;
  label: string | null;
  target: string | null;
}

export type MutationRungReceiptStageId =
  | 'control_plane'
  | 'host_process'
  | 'broker'
  | 'account_safety'
  | 'account_clerk'
  | 'reconciliation'
  | 'preflight'
  | 'trading_session'
  | 'runtime_freshness';

export interface MutationRungReceipt {
  code: MutationRungReceiptCode;
  tier: OperatorNoticeTier;
  title: string;
  message: string;
  rung_id: MutationRungReceiptStageId | null;
  source_codes: string[];
  forensic_facts: Record<string, string | number | boolean | null>;
  actionability: OperatorNoticeActionability;
  resolution: string;
  remedy_status: OperatorNoticeRemedyStatus | null;
  action: OperatorNoticeAction;
  occurred_at_ms: number;
}

export interface LogLine {
  ts_ms: number | null;
  raw_text: string;
  event_type: 'bar' | 'raw';
  consolidator_emitted: number | null;
  snapshot_set: string | null;
}

export type BotEventType =
  | 'evaluation_idle'
  | 'signal_fired'
  | 'order_submitted'
  | 'order_filled'
  | 'order_cancelled'
  | 'order_rejected'
  | 'blocked'
  | 'halted'
  | 'launch_failed';

export type BotEventSeverity = 'info' | 'warning' | 'critical';

export type SourceAuthority =
  | 'engine_loop'
  | 'daemon_launcher'
  | 'broker_session'
  // Historical BotEvent token; changing it would corrupt replayed audit data.
  | 'account_owner';

export type GateStepResult = 'pass' | 'skip' | 'block';

export type TerminalErrorCode =
  | 'order_rejected'
  | 'submit_uncertain'
  | 'halted'
  | 'launch_failed'
  | 'unmapped_diagnostic';

export type TerminalErrorSource =
  | 'engine'
  | 'ibkr'
  | 'daemon'
  | 'os'
  | 'broker_session'
  | 'unknown';

export type BotEventFactValue =
  | string
  | number
  | boolean
  | null
  | unknown[]
  | Record<string, unknown>;

export interface BotEventIdentity {
  evaluation_id: string | null;
  intent_id: string | null;
  order_ref: string | null;
  req_id: number | null;
  order_id: number | null;
  perm_id: number | null;
  exec_id: string | null;
}

export interface GateStep {
  evaluation_id: string;
  gate_id: string;
  gate_result: GateStepResult;
  source_authority: SourceAuthority;
  facts: Record<string, BotEventFactValue>;
}

export interface TerminalError {
  code: TerminalErrorCode;
  source: TerminalErrorSource;
  gate_id: string | null;
  message: string;
  detail: string | null;
  external_code: string | number | null;
  external_message: string | null;
  cause_chain: string[];
  forensic_facts: Record<string, BotEventFactValue>;
}

export interface BotEventRow {
  schema_version: number;
  seq: number;
  ts_ms: number;
  event_type: BotEventType;
  source_authority: SourceAuthority;
  identity: BotEventIdentity;
  severity: BotEventSeverity;
  headline: string;
  narrative: string;
  gate_steps: GateStep[];
  terminal_error: TerminalError | null;
  facts: Record<string, BotEventFactValue>;
}

export interface BotEventPage {
  rows: BotEventRow[];
  next_seq: number | null;
  durable_stream_id: string;
  high_water_cursor: string;
  next_cursor: string | null;
}

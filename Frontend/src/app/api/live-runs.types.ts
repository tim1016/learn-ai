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
// Canonical control-plane primitives live in `live-runs-controls.types.ts`,
// which is owned by the sibling backend PR (`prd-a/ui-1-status-and-controls-api`,
// #390). They are imported here and re-exported so existing imports from this
// module keep working while the single source of truth for the control-plane
// contract stays in one file.
import type {
  CommandVerb,
  DesiredStateAction,
  DesiredStateView,
} from './live-runs-controls.types';

export type {
  CommandVerb,
  DesiredStateAction,
  DesiredStatePathStatus,
  DesiredStateValue,
} from './live-runs-controls.types';

/**
 * Resolved durable-intent view consumed by the UI. Structurally identical to
 * `DesiredStateView` from the control-plane contract; aliased so the UI layer
 * has a stable local name.
 */
export type DesiredState = DesiredStateView;

export interface DesiredStateWriteRequest {
  action: DesiredStateAction;
  reason?: string;
}

export interface DesiredStateWriteResponse {
  accepted: boolean;
  desired_state: DesiredState;
}

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
  /** The SHA the daemon process is actually RUNNING (captured at launch; null if
   * git unavailable). The daemon does not reload on `git pull`. */
  git_sha?: string | null;
  /** The live on-disk HEAD — what a restart would run. */
  repo_head_sha?: string | null;
  /** True when the running code differs from the working tree (restart to apply). */
  code_stale?: boolean;
  /** Best-effort count of how many commits behind the working tree the running
   * code is (null when equal/unknown). */
  commits_behind?: number | null;
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

// ─────────────────────────── ADR 0009 sizing policy ───────────────────────────
//
// `live_config.sizing` is the operator-selected policy that governs `set_holdings`
// at the engine boundary. The discriminated union mirrors Python's
// `app.engine.execution.order_sizer.SizingPolicy` 1:1.
//
// PR1 ships only `FixedShares` at runtime; the other kinds validate but are
// disabled in the deploy form UI:
//   * `SetHoldings` (Reference parity)  — wired in PR3
//   * `FixedNotional` (Custom: notional) — wired in PR4
//   * `StrategyExplicit`                 — surfaced in PR7

export type SizingKind = 'FixedShares' | 'SetHoldings' | 'FixedNotional' | 'StrategyExplicit';

export interface SizingFixedShares {
  kind: 'FixedShares';
  value: number;
}

export interface SizingSetHoldings {
  kind: 'SetHoldings';
  /** Decimal string on the wire (never a float) — preserves run_id hash stability. */
  fraction: string;
}

export interface SizingFixedNotional {
  kind: 'FixedNotional';
  /** Decimal string on the wire (never a float) — preserves run_id hash stability. */
  value: string;
}

export interface SizingStrategyExplicit {
  kind: 'StrategyExplicit';
}

export type SizingPolicy =
  | SizingFixedShares
  | SizingSetHoldings
  | SizingFixedNotional
  | SizingStrategyExplicit;

/** Deploy-form preset (ADR 0009 § 7). Each maps to one or two `SizingPolicy`
 * shapes. Reference parity is gated by the audit-copy allow-list (PR3). */
export type SizingPreset = 'safe_canary' | 'reference_parity' | 'custom';

/** Engine-derived sizing stamps on the run ledger (ADR 0009 § 3). Never operator
 * input; the engine derives both at deploy/start. */
export type GovernedBy = 'live_config' | 'strategy_explicit';
export type SizingProvenance = 'reference_native' | 'live_override' | 'spec_default';

/** Deploy (create-a-run) request — forwarded by the data plane to the daemon
 * (ADR 0006). The QC anchor (`qc_cloud_backtest_id` + `qc_audit_copy_path`) is
 * mandatory by design. `start: true` chains a launch using `start_options`. */
export interface HostRunnerDeployRequest {
  strategy_spec_path: string;
  qc_audit_copy_path: string;
  qc_cloud_backtest_id: string;
  account_id: string;
  start_date_ms: number;
  strategy_instance_id: string;
  /** Algorithm module the run is reconciled to (#416); pins the Start guard. */
  strategy_key: string;
  live_config?: Record<string, unknown>;
  force?: boolean;
  start?: boolean;
  start_options?: HostRunnerStartRequest;
}

export interface HostRunnerDeployResponse {
  run_id: string;
  run_dir: string;
  /** False for an idempotent no-op (the run already existed with a matching ledger). */
  created: boolean;
  start: HostRunnerActionResponse | null;
}

/** Committed QC audit copies under `references/qc-shadow` (the deploy picker). */
export interface QcAuditCopyListing {
  scope_root: string;
  entries: string[];
}

/** Minimal strategy descriptor from `GET /api/engine/strategies`. `name` is the
 * algorithm module (the `strategy_key`); the full payload also carries a params
 * schema the deploy form does not need. */
export interface EngineStrategyInfo {
  name: string;
  display_name: string;
  description: string;
}

/** Canonical strategy-spec fixture from `GET /api/spec-strategy/fixtures`.
 * `path` is repo-relative and can be passed back to deploy as
 * `strategy_spec_path`. */
export interface SpecStrategyFixture {
  name: string;
  spec_name: string;
  path: string;
  symbols: string[];
  description: string | null;
}

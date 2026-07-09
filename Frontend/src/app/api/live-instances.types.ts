// Instance-addressed operator console types (ADR 0004).
// The console's subject is the strategy instance; the current run is evidence.
import type {
  GovernedBy,
  HostRunnerStartRequest,
  HydratePolicy,
  MutationRungReceipt,
  OperatorNoticeAction,
  OperatorNoticeActionability,
  OperatorNoticeRemedyStatus,
  OperatorNoticeTier,
  SizingPolicy,
  SizingPreset,
  SizingProvenance,
} from './live-runs.types';
import type { DesiredStateView } from './live-runs-controls.types';
import type { ActionPlan } from './action-plan.types';
import type { BotLifecycleChartView } from './lifecycle-projection.types';
import type {
  OperatorSurfaceBlockageLadder,
  OperatorSurfaceNamedCondition,
} from './operator-observability.types';

export type {
  MutationRungReceipt,
  MutationRungReceiptCode,
  MutationRungReceiptStageId,
  OperatorNoticeAction,
  OperatorNoticeActionability,
  OperatorNoticeActionKind,
  OperatorNoticeRemedyStatus,
  OperatorNoticeTier,
} from './live-runs.types';
export type {
  BotLifecycleChartView,
  LifecycleChartAction,
  LifecycleChartActionId,
  LifecycleChartActionability,
  LifecycleChartEdge,
  LifecycleChartGraph,
  LifecycleChartLane,
  LifecycleChartNode,
  LifecycleChartReceipt,
  LifecycleChartStatus,
  LifecycleEventCategory,
  LifecycleEventSeverity,
  LifecycleProjectionEventRow,
  LifecycleSafetySeverity,
  LifecycleSafetyTriageResponse,
  LifecycleTimelineResponse,
} from './lifecycle-projection.types';
export type {
  BrokerConnectionConditionCode,
  OperatorSurfaceBlockageLadder,
  OperatorSurfaceBlockageStage,
  OperatorSurfaceBlockageStageId,
  OperatorSurfaceBlockageState,
  OperatorSurfaceConditionSeverity,
  OperatorSurfaceNamedCondition,
} from './operator-observability.types';

export type InstanceProcessState =
  | 'running'
  | 'stopping'
  | 'exited'
  | 'idle'
  | 'unreachable';

export interface InstanceProcessView {
  state: InstanceProcessState;
  pid?: number | null;
  bound_run_id?: string | null;
  started_at_ms?: number | null;
}

/** The run an instance is writing to right now (registry-sourced). Commands route here only. */
export interface LiveBinding {
  run_id: string;
  run_dir?: string | null;
  source: string;
}

/** The instance's latest run by ledger — evidence only, never live. */
export interface EvidenceBinding {
  run_id: string;
  state: string;
  is_live: boolean;
}

// --- Readiness vector (ADR 0005) ---

export type ReadinessVerdict = 'READY' | 'BLOCKED' | 'DEGRADED' | 'UNKNOWN';

export type BotLifecyclePhaseValue = 'OFF_DUTY' | 'ON_DUTY' | 'RETIRED';
export type BotLifecyclePresenceLabel = 'Off duty' | 'On duty' | 'Retired';
export type BotLifecycleDisplayStatus =
  | 'Off duty'
  | 'Ready'
  | 'On duty'
  | 'Clocking out'
  | 'Sick bay'
  | 'Off roster'
  | 'Retired';
export type BotLifecycleActionId =
  | 'confirm_start'
  | 'end_day_now'
  | 'retire_replace'
  | 'add_to_roster'
  | 'take_off_roster';

export interface BotLifecycleAction {
  id: BotLifecycleActionId;
  label: string;
  enabled: boolean;
  reason: string | null;
  offer_id: string | null;
  expires_at_ms: number | null;
}

export interface BotLifecycleCondition {
  scope: 'account' | 'bot';
  severity: 'warning' | 'critical';
  title: string;
  detail: string;
  owner_label: string;
  cure_action: string;
  cure_label: string;
}

export interface BotDailyLifecycleProjection {
  phase: BotLifecyclePhaseValue;
  presence_label: BotLifecyclePresenceLabel;
  display_status: BotLifecycleDisplayStatus;
  attention_badge: 'Sick bay' | 'Ready' | 'Off roster' | null;
  reason: string | null;
  on_roster: boolean;
  active_run_id: string | null;
  latest_run_id: string | null;
  drift_detected: boolean;
  conditions?: BotLifecycleCondition[];
  primary_action: BotLifecycleAction | null;
  ambient_actions: BotLifecycleAction[];
}

export interface BotLifecycleRosterRequest {
  on_roster: boolean;
  updated_by?: string;
  reason?: string | null;
}

export interface BotRetireReplaceRequest {
  confirm_account_flat: boolean;
  replacement_requested?: boolean;
  updated_by?: string;
  reason?: string;
}

export interface BotLifecycleMutationResponse {
  strategy_instance_id: string;
  lifecycle: BotDailyLifecycleProjection;
}

export interface BotRollCallSummary {
  ready: number;
  off_roster: number;
  sick_bay: number;
  on_duty: number;
  off_duty: number;
  retired: number;
  generated_at_ms: number | null;
  session_date: string | null;
  effective_stop_ms: number | null;
}

export interface BotRollCallOffer {
  offer_id: string;
  strategy_instance_id: string;
  run_id: string;
  session_date: string;
  issued_at_ms: number;
  expires_at_ms: number;
}

export interface BotRollCallResponse {
  summary: BotRollCallSummary;
  offers: BotRollCallOffer[];
}

export interface ReadinessGate {
  name: string;
  status: 'pass' | 'fail' | 'unknown';
  severity: 'hard' | 'soft';
  detail: string;
  gate_result?: GateResult | null;
}

export interface ReadinessVector {
  kind: 'live_readiness' | 'start_readiness';
  as_of_ms: number;
  source: 'engine' | 'backend_derived';
  verdict: ReadinessVerdict;
  summary: string;
  gates: ReadinessGate[];
  live_readiness_available?: boolean | null;
  orders_used?: number | null;
  orders_cap?: number | null;
}

export interface DecisionColumnDescriptor {
  name: string;
  label: string;
  type: string;
  format: string;
  semantic?: string;
}

export type LatestSignalTone = 'ok' | 'warn' | 'neutral';

/** The instance's namespace-attributed broker slice (ADR 0005, #398). */
export interface InstanceBrokerView {
  bot_order_namespace: string;
  owned_positions: Record<string, number>;
  pending_order_count: number;
}

// --- Fleet/account contamination (ADR 0005, #399) ---

export interface FleetExplainedBucket {
  strategy_instance_id: string;
  positions: Record<string, number>;
}

export interface FleetContamination {
  net_positions: Record<string, number> | null;
  explained_total: Record<string, number>;
  explained_by_instance: FleetExplainedBucket[];
  residual: Record<string, number>;
  verdict: 'clean' | 'contaminated' | 'unknown';
  policy_blocks_starts: boolean;
  summary: string;
}

/** Pre-filled Start-card values for the console (#416). `strategy` is seeded
 * from the run ledger's `strategy_key`; empty means a legacy ledger with no
 * recorded key. The other four mirror the daemon start-request defaults. */
export interface InstanceStartDefaults {
  strategy: string;
  readonly: boolean;
  hydrate_policy: HydratePolicy;
  max_orders_per_day: number;
  ibkr_host: string;
  /** The bound run's ledger deploy identity, for a one-click re-deploy (fresh
   * run_id) of a poisoned/halted instance. Empty for legacy ledgers. */
  strategy_spec_path?: string;
  qc_audit_copy_path?: string;
  qc_cloud_backtest_id?: string;
  account_id?: string;
}

export interface LiveInstanceStatus {
  strategy_instance_id: string;
  process: InstanceProcessView;
  live_binding: LiveBinding | null;
  evidence_binding: EvidenceBinding | null;
  desired_state: DesiredStateView | null;
  readiness: ReadinessVector | null;
  latest_decision: Record<string, unknown> | null;
  latest_signal_tone: LatestSignalTone;
  decision_columns: DecisionColumnDescriptor[];
  broker: InstanceBrokerView | null;
  /** Pre-filled Start-card values (#416); null when nothing is deployed. */
  start_defaults: InstanceStartDefaults | null;
  /** What the run's content-addressed identity attests to (the hashed deploy
   * inputs); null when nothing is deployed. Drives the "what this proves" card. */
  provenance: InstanceProvenance | null;
  /** ADR 0009 — sizing surface for the Sizing card. Null when nothing is
   * deployed; a legacy/pre-policy run surfaces with `policy = null` (the UI
   * shows the honest "Pre-policy run" degraded badge). */
  sizing: InstanceSizing | null;
  /** Why the most recent run ended; null while a run is live or nothing was
   * ever deployed. Drives the console's "why it stopped" surface. */
  last_exit: InstanceLastExit | null;
  /** Monitor/chart symbol sourced from the action-plan traded stock when
   * present, then ``live_config.symbol`` for legacy signal=trade runs.
   * ``null`` when nothing is deployed or the ledger predates the symbol field —
   * consumers must treat null as "unknown" and NOT fall back to a hardcoded
   * default (the prior 'SPY' default was the bug Slice 2 closes). */
  symbol: string | null;
  /** PRD #593 Slice 1A — operator-declared instrument plan for the bound
   * run, sourced from ``ledger.live_config.action``. ``null`` when nothing
   * is deployed OR the ledger pre-dates the field — the cockpit must
   * distinguish "declared empty" (`{on_enter: [], on_exit: []}`) from
   * "pre-Slice-1A ledger" (null). Engine consumption is Slice 4. */
  action_plan: ActionPlan | null;
  /** PRD #593 Slice 1A — the strategy registry's ``instrument_surface``
   * value for the bound run's strategy. Informational in Slices 1–3
   * (every current strategy is ``explicit``). ``null`` when nothing is
   * deployed or the strategy isn't in the registry. */
  instrument_surface: 'policy' | 'explicit' | null;
  /** PRD #593 Slice 1E (#598) / ADR 0012 §7 — unhashed redeploy lineage
   * sourced from the ledger's ``lineage`` block. ``null`` when nothing
   * is deployed or the ledger pre-dates the field. */
  lineage: ActionPlanLineage | null;
  /** PRD #607 / Slice 1 (#608) — server-authored operator-facing
   * projection.  Always present (never null); per-block fields may be
   * null per the documented semantics.  Frontend renders these fields;
   * it does NOT derive verdicts from raw fields. */
  operator_surface: OperatorSurface;
  /** Backend-authored chart contract for the Overview tab. The frontend
   * renders nodes, edges, statuses, and action enablement verbatim. */
  lifecycle_chart: BotLifecycleChartView;
  /** Rev 3 daily lifecycle projection: three phases, closed display
   * vocabulary, roster flag, and Button Rule action ids. */
  daily_lifecycle: BotDailyLifecycleProjection;
  fetched_at_ms: number;
}

// PRD #607 / Slice 1 (#608) — operator surface projection types.

export type OperatorVerdict = 'READY' | 'ATTENTION' | 'UNKNOWN';

export type HostProcessState =
  | 'RUNNING'
  | 'STOPPING'
  | 'EXITED'
  | 'IDLE'
  | 'WAITING_FOR_HOST'
  | 'UNREACHABLE';

export type PriorRunClassification =
  | 'CLEAN'
  | 'HALT_TRIGGERED'
  | 'EXITED_WITH_ERROR'
  | 'UNKNOWN';

export type BrokerSafetyVerdict = 'PAPER_ONLY' | 'UNSAFE' | 'UNKNOWN';

export type BrokerConnectionState = 'CONNECTED' | 'DISCONNECTED' | 'DEGRADED' | 'UNKNOWN';
export type ExecutionPosture =
  | 'PAPER_EXECUTION'
  | 'READ_ONLY'
  | 'UNSAFE'
  | 'UNKNOWN';

export type TradingSessionPhase =
  | 'PRE'
  | 'RTH'
  | 'POST'
  | 'CLOSED'
  | 'UNKNOWN';

export type AccountOwnerPhase = 'accepting' | 'reconnecting' | 'draining' | 'frozen' | 'unknown';

export type SubmitReadinessCode =
  | 'safe_to_submit'
  | 'safe_to_monitor'
  | 'blocked_before_submit'
  | 'broker_state_unproven'
  | 'account_frozen'
  | 'waiting_for_owner_generation'
  | 'submit_outcome_uncertain';

export type TraderSituationCode =
  | 'ready_to_submit'
  | 'monitor_only'
  | 'submission_blocked'
  | 'broker_state_unproven'
  | 'account_frozen'
  | 'waiting_for_owner_generation'
  | 'submit_outcome_uncertain'
  | 'attention_required'
  | 'unknown';

export type TraderAttentionSeverity = 'info' | 'warning' | 'critical';

export type RiskPosture = 'FLAT' | 'LONG' | 'SHORT' | 'MIXED' | 'UNKNOWN';

export type ActionPlanConsumption = 'ACTIVE' | 'DECLARATIVE_ONLY' | 'UNKNOWN';

export type ActionEffect = 'DURABLE_ONLY' | 'LIVE_ACTUATION';

export type GateResultStatus =
  | 'pass'
  | 'block'
  | 'poison'
  | 'freeze'
  | 'unknown'
  | 'not_applicable';

export interface GateResult {
  gate_id: string;
  status: GateResultStatus;
  source: string;
  operator_reason: string;
  operator_next_step: string | null;
  evidence_at_ms: number;
}

export interface ActionCapability {
  enabled: boolean;
  effect: ActionEffect;
  /** PRD #616 — single-line tooltip code (head of `disabled_reasons` after
   *  priority sort).  `null` when `enabled` is true. */
  disabled_reason_code: string | null;
  /** PRD #616 / PRD #619-A — full priority-ordered list of applicable
   *  reason codes.  Empty (but present) when `enabled` is true.  Now
   *  required: every pre-#616 fixture has been updated to emit `[]`
   *  for the empty case so the optional shape is no longer needed
   *  (PRD #619-A §A6). */
  disabled_reasons: string[];
  gate_results: GateResult[];
}

export type HostProcessStartDisabledReasonCode =
  | 'ALREADY_RUNNING'
  | 'STOPPING'
  | 'HOST_SERVICE_OFFLINE'
  | 'STOPPED_REQUIRES_RESUME'
  | 'STOPPED_REQUIRES_REDEPLOY'
  | 'START_SETTINGS_INCOMPLETE'
  | 'ACCOUNT_FROZEN'
  | 'CRASH_RECOVERY_REQUIRED';

export interface CrashRecoveryOverrideRequest {
  confirm_account_flat: true;
  approved_by?: string;
  reason?: string | null;
}

export interface CrashRecoveryOverrideResponse {
  accepted: true;
  account_id: string;
  strategy_instance_id: string;
  run_id: string;
  bot_order_namespace: string;
  override_id: string;
  recorded_at_ms: number;
  blocking_recorded_at_ms: number;
  event_type: 'account_audited_override_recorded';
  // Backend always serializes warnings (default []); only the receipt itself is
  // nullable (absent when post-commit projection degrades — see the override endpoint).
  rung_receipt?: MutationRungReceipt | null;
  rung_receipt_warnings: MutationRungReceipt[];
}

/** Server-authored per-instance Start-bot-process affordance
 *  (ADR-0006 §1 / ADR-0007 / ADR 0013 amendment 2026-06-22).
 *
 *  Drives the cockpit's "Start bot process" button. The data-plane proxy
 *  re-runs the same enable check before forwarding to the daemon, so a
 *  stale ``enabled: true`` cannot bypass the gate. When enabled,
 *  ``run_id`` and ``request`` together carry the exact POST the cockpit
 *  will fire — Angular MUST NOT compose the body. */
export interface HostProcessStartCapability {
  enabled: boolean;
  /** Target of ``POST /runs/{run_id}/start``. Populated only when
   *  ``enabled`` is true. */
  run_id: string | null;
  /** Server-authored request body. Absent when ``enabled`` is false. */
  request: HostRunnerStartRequest | null;
  /** Closed reason code; present iff ``enabled`` is false. */
  disabled_reason_code: HostProcessStartDisabledReasonCode | null;
  gate_results: GateResult[];
}

export interface OperatorSurfaceHostProcess {
  state: HostProcessState;
  /** Operator-language line authored server-side when state != RUNNING.  ``null`` when running. */
  notice: string | null;
  /** Exact host command the operator can paste, ONLY for UNREACHABLE
   *  when trusted deployment configuration supplies a non-empty value.
   *  Angular renders verbatim and MUST NOT construct, interpolate, or
   *  transform this string. */
  copyable_command: string | null;
  /** Typed last-exit evidence promoted from `run_status.json`.
   *  Keeps already-exited startup failures specific in the cockpit. */
  last_exit_error_code: string | null;
  last_exit_error_message: string | null;
  last_exit_error_detail: Record<string, unknown>;
  /** Per-instance Start-bot-process button. Always present so the
   *  cockpit can render a disabled state with a server-authored reason. */
  start_capability: HostProcessStartCapability;
}

export interface OperatorSurfacePriorRun {
  classification: PriorRunClassification;
}

/** PRD #718 — AccountOwner generation/phase surfaced from canonical
 * account artifacts. `phase = unknown` means missing proof, not healthy. */
export interface OperatorSurfaceAccountOwner {
  account_id: string;
  generation: number | null;
  phase: AccountOwnerPhase;
  recorded_at_ms: number | null;
  source: string | null;
}

export interface OperatorSurfaceBroker {
  safety_verdict: BrokerSafetyVerdict;
  /** Independent of safety_verdict: whether the broker session is up.
   *  A paper-only account whose IBKR session has dropped is
   *  ``safety_verdict=PAPER_ONLY`` AND ``connection=DISCONNECTED``;
   *  composing them is forbidden. */
  connection: BrokerConnectionState;
  connection_condition: OperatorSurfaceNamedCondition;
}

export interface OperatorSurfaceExecution {
  posture: ExecutionPosture;
}

export interface OperatorSurfaceTradingSession {
  phase: TradingSessionPhase;
  /** Server-derived: phase + strategy's session policy.  Frontend does
   *  not derive this from the phase enum. */
  permits_strategy_activity: boolean | null;
  next_transition_ms: number | null;
  timezone: string;
  as_of_ms: number;
}

export interface OperatorSurfaceCurrentRisk {
  posture: RiskPosture;
  owned_positions: Record<string, number>;
  /** ``null`` when broker state is unavailable; ``0`` only when known empty. */
  pending_order_count: number | null;
  verdict: OperatorVerdict;
  /** ``null`` when the broker connector cannot supply a value. */
  unrealized_pnl: number | null;
}

export interface OperatorSurfaceDailyOrderCap {
  used: number | null;
  limit: number | null;
}

export interface OperatorSurfaceActionPlan {
  consumption: ActionPlanConsumption;
  anomaly_verdict: OperatorVerdict;
}

export interface OperatorSurfaceConfiguration {
  verdict: OperatorVerdict;
  reason_codes: string[];
}

export interface OperatorSurfaceActions {
  resume: ActionCapability;
  pause: ActionCapability;
  /** PRD #616 / PRD #619-A — fifth canonical action (ADR-0010 §A1).
   *  Required: every cockpit fixture has been updated to emit a
   *  concrete capability; the pre-#616 optionality is removed in
   *  PRD #619-A §A6. */
  stop: ActionCapability;
  flatten_and_pause: ActionCapability;
  mark_poisoned: ActionCapability;
}

// PRD #616 — closed discriminated union for the server-authored
// suggested fix on a non-passing readiness gate.  The cockpit MUST
// match on `kind` exhaustively; an unknown kind fails closed visibly.

export interface InvokeCapabilityAction {
  kind: 'invoke_capability';
  /** Non-destructive only.  Destructive actions never appear via
   *  invoke_capability; they reach the operator via focus_action so
   *  they retain their canonical render site (ADR 0010 §A2). */
  capability: 'resume' | 'pause';
}

export interface FocusAction {
  kind: 'focus_action';
  tab: 'status' | 'activity' | 'audit' | 'configuration';
  action: 'flatten_and_pause' | 'stop' | 'mark_poisoned';
}

export interface RedeployAction {
  kind: 'redeploy';
}

export interface OpenRunbookAction {
  kind: 'open_runbook';
  slug: string;
}

export interface InvokeEndpointAction {
  kind: 'invoke_endpoint';
  endpoint: 'reconcile_instance';
  method: 'POST';
  path_template: '/api/live-instances/{strategy_instance_id}/reconcile';
}

export interface NoPrimaryRemediationAction {
  kind: 'none';
  reason: string;
}

export type GateSuggestedAction =
  | InvokeCapabilityAction
  | FocusAction
  | RedeployAction
  | OpenRunbookAction;

export type TraderPrimaryRemediation =
  | GateSuggestedAction
  | InvokeEndpointAction
  | NoPrimaryRemediationAction;

export interface OperatorSurfaceEvidenceFact {
  label: string;
  value: string;
  source: string | null;
  gate_id: string | null;
  ts_ms: number | null;
  ts_ms_resolved: boolean;
}

export interface OperatorSurfaceAttentionGroup {
  code: string;
  severity: TraderAttentionSeverity;
  headline: string;
  explanation: string;
  operator_next_step: string;
  remediation: TraderPrimaryRemediation;
}

export interface OperatorSurfaceProofLine {
  id: string;
  label: string;
  message: string;
  detail: string;
  tone: 'neutral' | 'ok' | 'attention';
}

export type OperatorSurfaceRunSignalTone = 'on' | 'off' | 'transition' | 'attention';

export interface OperatorSurfaceRunSignal {
  state_label: string;
  tone: OperatorSurfaceRunSignalTone;
  title: string;
  detail: string;
}

export interface OperatorSurfaceSubmitReadiness {
  code: SubmitReadinessCode;
  label: string;
  explanation: string;
  can_submit: boolean;
  blocking_reason_codes: string[];
  template_id: string;
  template_version: number;
}

export interface OperatorSurfaceTraderGuidance {
  situation_code: TraderSituationCode;
  headline: string;
  explanation: string;
  risk_headline: string;
  risk_explanation: string;
  primary_remediation: TraderPrimaryRemediation;
  additional_attention_groups: OperatorSurfaceAttentionGroup[];
  proof_lines: OperatorSurfaceProofLine[];
  advanced_evidence: OperatorSurfaceEvidenceFact[];
  template_id: string;
  template_version: number;
}

export interface OperatorGate {
  name: string;
  status: string;
  severity: string;
  detail: string;
  gate_result: GateResult;
  /** Either a structured suggested-action OR null + an explicit unavailable reason.
   *  Never null without a reason. */
  suggested_action: GateSuggestedAction | null;
  suggested_action_unavailable_reason: string | null;
}

export type RuntimeFreshnessState =
  | 'FRESH'
  | 'STALE'
  | 'NOT_APPLICABLE'
  | 'UNKNOWN'
  | 'DEGRADED';

// PRD operator-notice PR 1 — OperatorNotice types mirroring
// PythonDataService/app/operator/notices/schema.py.
// OperatorNoticeCode MUST stay byte-identical to the Python Literal and
// PythonDataService/app/operator/notices/snapshot.json (enforced by CI).

export type OperatorNoticeCode =
  // PR 1 — runtime freshness (implemented in this PR).
  | 'runtime.market_closed'
  | 'runtime.market_session_halted'
  | 'runtime.market_data_stale'
  | 'runtime.market_data_first_bar_timeout'
  | 'runtime.market_data_feed_stalled'
  | 'runtime.broker_probe_stale'
  | 'runtime.broker_probe_missing'
  | 'runtime.command_loop_unresponsive'
  | 'runtime.engine_runtime_incompatible'
  | 'runtime.control_plane_lease_stale'
  | 'runtime.control_plane_boot_id_mismatch'
  // PR 2 — watchdog two-phase halt (reserved).
  | 'watchdog.flatten_completed'
  | 'watchdog.flatten_not_needed'
  | 'watchdog.flatten_timed_out'
  | 'watchdog.flatten_failed'
  | 'watchdog.broker_disconnected_before_flatten'
  // PR 5 — activity health (reserved).
  | 'activity.publisher_starting'
  | 'activity.publisher_not_running'
  | 'activity.publisher_degraded'
  | 'activity.source_blind_to_bot_orders'
  | 'activity.dropped_paused_intent'
  // PR 6 — reconciliation (reserved).
  | 'reconciliation.required_after_uncertain_flatten'
  | 'reconciliation.discovered_execution_not_in_engine_state'
  | 'reconciliation.divergence_while_submitting'
  | 'fleet.sibling_liveness_unproven'
  // Broker session mirror — ADR 0018 orphaned-socket observability.
  | 'broker_session.orphaned_socket'
  // PRD #928 / ADR 0024 — order and submit terminal outcomes (reserved).
  | 'order.rejected'
  | 'submit.uncertain'
  | 'submit.halted'
  | 'submit.launch_failed'
  | 'submit.unmapped_diagnostic'
  | 'safety_halt.poisoned';

export interface OperatorNotice {
  code: OperatorNoticeCode;
  tier: OperatorNoticeTier;
  title: string;
  message: string;
  source_codes: string[];
  forensic_facts: Record<string, string | number | boolean | null>;
  actionability: OperatorNoticeActionability;
  resolution: string;
  remedy_status: OperatorNoticeRemedyStatus | null;
  action: OperatorNoticeAction;
  runbook_slug: string | null;
  occurred_at_ms: number | null;
}

export interface OperatorIncident {
  schema_version: number;
  incident_id: string;
  category: 'watchdog' | 'activity' | 'reconciliation' | 'order' | 'submit' | 'safety-halt';
  notice: OperatorNotice;
  started_at_ms: number;
  resolved_at_ms: number | null;
  evidence: Record<string, unknown>;
}

export interface OperatorSurfaceDomainFreshness {
  state: RuntimeFreshnessState;
  age_ms: number | null;
  stale_reason_codes: string[];
}

export interface OperatorSurfaceRuntimeFreshness {
  posture_demoted: boolean;
  stale_reason_codes: string[];
  command_loop: OperatorSurfaceDomainFreshness;
  broker: OperatorSurfaceDomainFreshness;
  bar_loop: OperatorSurfaceDomainFreshness;
  control_plane: OperatorSurfaceDomainFreshness;
  /** Backend-authored top-priority notice for the banner; null when all
   *  active rules are banner-suppressed (e.g. market closed). */
  headline: OperatorNotice | null;
  /** Active runtime-freshness notices excluding the headline (backend
   *  pre-filtered). Empty when all domains are fresh or only the headline
   *  matched. Cockpit renders these in the detail panel without deduplication. */
  additional_reasons: OperatorNotice[];
}

/** Closed-kind union for the typed daemon transport outcome (PRD #619-C1).
 *  Mirrors the backend ``DaemonResultKind``. */
export type DaemonResultKind =
  | 'CONNECTED'
  | 'RETRYING'
  | 'UNREACHABLE'
  | 'AUTH_FAILED'
  | 'PROTOCOL_ERROR'
  | 'INCOMPATIBLE_CONTRACT';

/** PRD #619-C3 — server-authored control-plane (host-daemon) connectivity
 *  surface. Distinct from ``broker.connection`` and ``host_process`` —
 *  three independent facts the operator reads separately.
 *
 *  ``state`` is the connectivity monitor's folded verdict (619-C2).
 *  ``notice`` and ``runbook_slug`` are server-authored; the cockpit
 *  renders them verbatim (no enum-to-string mapping). */
export interface OperatorSurfaceControlPlane {
  state: DaemonResultKind;
  last_transition_ms: number | null;
  last_success_ms: number | null;
  attempt: number;
  daemon_boot_id: string | null;
  notice: string | null;
  runbook_slug: string | null;
}

/** PR 5 — raw diagnostic facts behind the broker-activity health verdict.
 *  Rendered in a forensic-detail panel only. The cockpit MUST NOT derive
 *  state from these fields — state comes from `BrokerActivityHealth.state`. */
export interface BrokerActivityHealthFacts {
  publisher_registered: boolean;
  publisher_running: boolean;
  latest_row_seq: number | null;
  seconds_since_registered: number | null;
  seconds_since_last_row: number | null;
}

/** PR 5 — typed broker-activity publisher health verdict.
 *
 *  States:
 *  - `ready`       — publisher registered + running + emitting rows (or in silent-boot window).
 *  - `starting`    — publisher registered but not yet running; within the starting-timeout.
 *  - `degraded`    — publisher running but no recent rows.
 *  - `unavailable` — publisher not registered or timed out while starting.
 */
export interface BrokerActivityHealth {
  state: 'ready' | 'starting' | 'degraded' | 'unavailable';
  /** Backend-authored notice for the primary health display; null when state is `ready`. */
  headline: OperatorNotice | null;
  /** All active notices for this health verdict (empty when state is `ready`). */
  notices: OperatorNotice[];
  facts: BrokerActivityHealthFacts;
}

export interface OperatorSurfaceNoticePlacement {
  banner: OperatorNotice | null;
  banner_fold_count: number;
  banner_folded: OperatorNotice[];
  attention: OperatorNotice[];
  quiet_status: OperatorNotice[];
}

export interface OperatorSurface {
  /** Bump on breaking shape changes; additive fields do NOT bump the version. */
  schema_version: number;
  host_process: OperatorSurfaceHostProcess;
  prior_run: OperatorSurfacePriorRun;
  broker: OperatorSurfaceBroker;
  /** Slice 2: backend-authored execution posture. Missing/null means the
   * frontend must not render an Execution chip or infer one locally. */
  execution?: OperatorSurfaceExecution | null;
  configuration: OperatorSurfaceConfiguration;
  current_risk: OperatorSurfaceCurrentRisk;
  daily_order_cap: OperatorSurfaceDailyOrderCap;
  action_plan: OperatorSurfaceActionPlan;
  /** PRD #718 — optional AccountOwner generation/phase evidence. */
  account_owner: OperatorSurfaceAccountOwner | null;
  /** PRD #718 — backend-authored submit-readiness answer. */
  submit_readiness: OperatorSurfaceSubmitReadiness;
  /** PRD #718 — backend-authored trader-language right-pane contract. */
  trader_guidance: OperatorSurfaceTraderGuidance;
  /** Backend-authored lifecycle/current-blockage ladder for the Overview pane. */
  blockage_ladder: OperatorSurfaceBlockageLadder;
  /** Backend-authored compact process signal rendered beside one-click lifecycle controls. */
  run_signal: OperatorSurfaceRunSignal;
  actions: OperatorSurfaceActions;
  trading_session: OperatorSurfaceTradingSession;
  /** PRD #616 — operator-facing projection of engine readiness gates with
   *  server-authored remediation metadata.  Empty list when no readiness
   *  vector is available.  Order preserves the engine's gate order. */
  readiness_gates: OperatorGate[];
  /** Child-authored runtime evidence composed by the backend. Null when
   * no child is currently bound to the instance. */
  runtime_freshness: OperatorSurfaceRuntimeFreshness | null;
  /** PRD #619-C3 — host-daemon connectivity surface from the
   *  connectivity monitor (619-C2). Null when the data plane was booted
   *  without a daemon URL (the cockpit hides the card). */
  control_plane: OperatorSurfaceControlPlane | null;
  /** PRD #619-D4 — divergence verdict between the child's broker
   *  observation and the data-plane singleton's. Null when the
   *  comparison is impossible (no live binding). Never overwrites the
   *  child's authoritative posture on `broker`. */
  broker_observation_consistency: BrokerObservationConsistency | null;
  /** ADR-0008 §5 / PR 1 — cold-start reconciliation projection. Null
   *  when the comparison is impossible (no live binding); otherwise the
   *  cockpit reads `state` directly to render the hazard banner. The
   *  cockpit does NOT derive its own state from raw receipt fields. */
  reconciliation: OperatorSurfaceReconciliation | null;
  /** PR 5 — broker-activity publisher health surface. Null when no live
   *  binding exists (no publisher is registered). The cockpit uses
   *  `state` to replace the implicit "Loading history…" spinner with a
   *  typed server-authored verdict. */
  broker_activity_health: BrokerActivityHealth | null;
  /** PR 2 — post-halt watchdog incident headline. Null when no unresolved
   *  uncertain-outcome incident exists. The cockpit renders this above
   *  the freshness headline in the runtime banner. */
  incident_headline: OperatorNotice | null;
  /** ADR-0025 / PRD #972 — backend-authored single-banner and notice placement. */
  notice_placement: OperatorSurfaceNoticePlacement;
}

/** ADR-0008 §5 / PR 1 — operator-facing cold-start reconciliation state
 *  composed by the backend from the on-disk receipt + current freshness
 *  inputs (WAL seq, run/namespace identity, broker event clock, TTL). */
export type ReconciliationState =
  | 'NOT_AVAILABLE'
  | 'IN_PROGRESS'
  | 'CLEAN'
  | 'ADOPTED'
  | 'STALE'
  | 'FAILED';

export interface OperatorSurfaceReconciliation {
  state: ReconciliationState;
  /** Populated only when `state === 'FAILED'`. */
  failure_reason: string | null;
  /** Populated only when `state === 'ADOPTED'` (or `STALE` after an
   *  adopted receipt) — the intent_ids the orchestrator recovered. */
  adopted_intent_ids: string[];
  last_reconcile_ms: number | null;
  sidecar_wal_seq: number | null;
  broker_observed_at_ms: number | null;
}

/** PRD #619-D4 — backend-authored divergence card.
 *
 * The child's observation (`engine_runtime.broker.connected_account`)
 * and the data plane's singleton observation should match when the
 * deployment is healthy. The four-way verdict surfaces the divergence
 * prominently on CONFLICTING without ever overwriting the child's
 * authoritative posture on `OperatorSurface.broker`.
 */
export interface BrokerObservationConsistency {
  verdict: 'CONSISTENT' | 'CONFLICTING' | 'UNKNOWN' | 'NOT_COMPARABLE';
  child_account: string | null;
  data_plane_account: string | null;
  reason_codes: string[];
  compared_at_ms: number;
}

// PRD #616 — fleet account altitude DTO (server-authored).  Separates
// account identity from position contamination; the cockpit's account
// row reads exactly this shape.

export type AccountIdentity = 'CONSISTENT' | 'CONFLICTING' | 'UNKNOWN';

export interface FleetAccountSummary {
  account_id: string | null;
  account_identity: AccountIdentity;
  /** Closed reason-code vocabulary: ACCOUNT_ID_MISSING,
   *  INSTANCE_ACCOUNT_MISMATCH, BROKER_ACCOUNT_UNAVAILABLE,
   *  BROKER_ACCOUNT_MISMATCH. */
  account_identity_reason_codes: string[];
  contamination: FleetContamination;
}

export type ReadinessVerdictEnum = 'READY' | 'BLOCKED' | 'DEGRADED' | 'UNKNOWN';

export interface ActionPlanLineage {
  parent_run_id: string | null;
  redeploy_reason: string | null;
  /** Wall-clock ``int64`` ms UTC when the redeploy was issued. */
  redeployed_at_ms: number | null;
}

/** ADR 0009 § 11 — one row of the per-trade audit list. The cockpit renders
 * these in the Sizing card's bottom section. */
export interface SizingAuditRow {
  ts_ms: number;
  symbol: string;
  policy_kind: string;
  policy_value: string;
  intended_qty: number;
  /** Null when the sizing policy can resolve without a bar price, e.g.
   * FixedShares. Render absence rather than inventing a price. */
  reference_price: string | null;
  sized_via: string;
  /** VCR-0003 last-mile — provenance stamp the engine mints at
   * policy-resolution time. One of {reference_native, live_override,
   * spec_default}. null for legacy SIZING_RESOLVED rows authored
   * before the field landed and for sizing-skip rows (their JSONL
   * predates this column). When null, the Sizing card may render an
   * "unknown" badge variant. */
  sizing_provenance_at_resolve_time?: string | null;
  /** Phase 8 / VCR-0003 — present (true) on rows folded from
   * sizing_skip.jsonl; absent / undefined on rows from
   * intent_events.jsonl SIZING_RESOLVED. */
  skipped?: boolean | null;
  /** Phase 8 / VCR-0003 — the operator-visible reason the policy
   * skipped (e.g. "target_equals_current", "zero_shares_while_flat").
   * Only meaningful when `skipped === true`. */
  skip_reason?: string | null;
}

/** ADR 0009 — the bound (or evidence) run's sizing surface for the Sizing card.
 * `policy` is null for a legacy/pre-policy run (ledger has no `sizing` key); the
 * UI renders the honest "Pre-policy run" degraded badge in that case. */
export interface InstanceSizing {
  /** Resolved policy from `live_config.sizing` — the same shape submitted at
   * deploy. Null = legacy/pre-policy run; never substitute a default. */
  policy: SizingPolicy | null;
  /** Operator-facing preset inferred from the policy shape; null for a
   * pre-policy run. `explicit` corresponds to a `StrategyExplicit` policy. */
  preset: SizingPreset | 'explicit' | null;
  /** Engine-derived stamp: who set the quantity (deploy-page policy vs the
   * strategy's own `market_order` / `contracts_per_trade`). */
  governed_by: GovernedBy;
  /** Engine-derived stamp: does the resolved sizing match the bound QC audit
   * copy? `live_override` = fail-closed default until PR3 wires the allow-list. */
  sizing_provenance: SizingProvenance;
  /** Per-trade audit list (newest first, capped at 50). Empty for runs that
   * predate the audit log. */
  per_trade_audit: SizingAuditRow[];
}

/** What a run's content-addressed identity (`run_id`) fingerprints — so the UI
 * can explain the hashes rather than dump them. Empty strings for fields a
 * legacy ledger did not record. */
export interface InstanceProvenance {
  run_id: string;
  schema_version: string;
  code_sha: string;
  strategy_spec_path: string;
  strategy_spec_sha256: string;
  qc_audit_copy_path: string;
  qc_audit_copy_sha256: string;
  qc_cloud_backtest_id: string;
  account_id: string;
  start_date_ms: number | null;
  created_at_ms: number | null;
  /** Runtime config hashed into run_id (symbol, force_flat_at, …); surfaced so
   * runs differing only in config don't show identical proofs. */
  live_config: Record<string, unknown>;
}

/** Why an instance's most recent (terminated) run ended. */
export interface InstanceLastExit {
  run_id: string;
  ended_at_ms: number | null;
  exit_code: number | null;
  exit_reason: string | null;
  /** From the indicator-state hydration receipt, when the run wrote one.
   * `hydration_accepted === false` with `hydration_failure_reason === 'missing'`
   * is the cold-start / seed-day case. */
  hydration_accepted: boolean | null;
  hydration_failure_reason: string | null;
  /** From poisoned.flag: the specific safety trigger that halted the run
   * (outside_mutation / lost_fill / cold_start_divergence / operator_declared)
   * + its forensic details. Null when the run left no poison flag. */
  halt_trigger: string | null;
  halt_at_ms: number | null;
  halt_detail: Record<string, unknown> | null;
}

export interface LiveInstanceSummary {
  strategy_instance_id: string;
  process_state: string;
  bound_run_id?: string | null;
  latest_run_id?: string | null;
  desired_state?: string | null;
  /** PRD #616 — per-instance readiness verdict so the outer-tab badge
   *  ("dep_val_smoke_001 · IDLE · BLOCKED") renders without an N+1 fetch.
   *  ``UNKNOWN`` when readiness cannot be resolved. */
  readiness_verdict?: ReadinessVerdictEnum;
  readiness_as_of_ms?: number | null;
}

export type BotCatalogTone = 'positive' | 'warning' | 'danger' | 'neutral';
export type BotCatalogTradingMode = 'paper' | 'live' | 'unknown';

export interface BotCatalogPnl {
  realized: number | null;
  unrealized: number | null;
  total: number | null;
}

export interface BotCatalogMetrics {
  pnl: BotCatalogPnl;
  trade_count: number | null;
  current_exposure: string;
  open_positions: number | null;
  error_count: number;
}

export type BotAttendanceStatus = 'clean' | 'rested' | 'sick' | 'retired';

export interface BotAttendanceCell {
  session_date: string;
  status: BotAttendanceStatus;
  label: string;
  receipt_ref: string | null;
}

export interface BotEveningReportRow {
  strategy_instance_id: string;
  label: string;
  status: BotAttendanceStatus;
  receipt_ref: string | null;
}

export interface BotEveningReport {
  session_date: string;
  generated_at_ms: number;
  clean_exits: number;
  rested: number;
  sick: number;
  retired: number;
  summary: string;
  rows: BotEveningReportRow[];
}

export interface BotCatalogRow {
  strategy_instance_id: string;
  name: string;
  description: string | null;
  status_label: string;
  status_detail: string | null;
  status_tone: BotCatalogTone;
  only_fresh_run_available: boolean;
  needs_attention: boolean;
  trading_mode: BotCatalogTradingMode;
  symbols: string[];
  engine: string | null;
  engine_asset_class: string | null;
  created_at_ms: number | null;
  updated_at_ms: number | null;
  last_run_at_ms: number | null;
  last_run_label: string;
  last_run_result: string;
  last_run_detail: string | null;
  process_state: string;
  desired_state: string | null;
  readiness_verdict: ReadinessVerdictEnum;
  daily_lifecycle: BotDailyLifecycleProjection;
  start_request: HostRunnerStartRequest | null;
  attendance: BotAttendanceCell[];
  metrics: BotCatalogMetrics;
}

export interface BotCatalogResponse {
  bots: BotCatalogRow[];
  roll_call: BotRollCallSummary;
  evening_report: BotEveningReport | null;
}

export interface BotDeleteRequest {
  mode?: 'soft';
  deleted_by?: string;
  reason?: string | null;
}

export interface BotDeleteResponse {
  strategy_instance_id: string;
  mode: 'soft';
  deleted_at_ms: number;
  deleted_by: string;
  reason: string | null;
  deleted_run_ids: string[];
  marker_path: string;
  hidden_from_catalog: boolean;
}

// --- Single operator intent knob (ADR 0004) ---

export type DesiredStateAction = 'pause' | 'resume' | 'stop';

export interface InstanceDesiredStateRequest {
  action: DesiredStateAction;
  reason?: string;
  updated_by?: string;
}

export interface IntentActuation {
  actuated: boolean;
  run_id?: string | null;
  command_seq?: number | null;
  detail: string;
}

export interface DesiredStateRecord {
  state: string;
  updated_at_ms: number;
  updated_by: string;
  reason?: string | null;
  version: number;
}

export interface SetInstanceDesiredStateResponse {
  durable: DesiredStateRecord;
  actuation: IntentActuation;
  rung_receipt: MutationRungReceipt;
  rung_receipt_warnings: MutationRungReceipt[];
}

// Instance-addressed operator console types (ADR 0004).
// The console's subject is the strategy instance; the current run is evidence.
import type {
  GovernedBy,
  HydratePolicy,
  SizingPolicy,
  SizingPreset,
  SizingProvenance,
} from './live-runs.types';
import type { DesiredStateView } from './live-runs-controls.types';
import type { ActionPlan } from './action-plan.types';

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

export interface ReadinessGate {
  name: string;
  status: 'pass' | 'fail' | 'unknown';
  severity: 'hard' | 'soft';
  detail: string;
}

export interface ReadinessVector {
  kind: 'live_readiness' | 'start_readiness';
  as_of_ms: number;
  source: 'engine' | 'backend_derived';
  verdict: ReadinessVerdict;
  summary: string;
  gates: ReadinessGate[];
  live_readiness_available?: boolean | null;
}

export interface DecisionColumnDescriptor {
  name: string;
  label: string;
  type: string;
  format: string;
  semantic?: string;
}

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
  /** Traded symbol sourced from the ledger's ``live_config.symbol`` (Slice 2).
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

export type BrokerConnectionState = 'CONNECTED' | 'DISCONNECTED' | 'UNKNOWN';

export type TradingSessionPhase =
  | 'PRE'
  | 'RTH'
  | 'POST'
  | 'CLOSED'
  | 'UNKNOWN';

export type RiskPosture = 'FLAT' | 'LONG' | 'SHORT' | 'MIXED' | 'UNKNOWN';

export type ActionPlanConsumption = 'ACTIVE' | 'DECLARATIVE_ONLY' | 'UNKNOWN';

export type ActionEffect = 'DURABLE_ONLY' | 'LIVE_ACTUATION';

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
}

export interface OperatorSurfaceHostProcess {
  state: HostProcessState;
  /** Operator-language line authored server-side when state != RUNNING.  ``null`` when running. */
  notice: string | null;
  /** Exact host command the operator can paste, ONLY when the server can author it safely.
   *  Angular renders verbatim and MUST NOT construct, interpolate, or transform this string. */
  copyable_command: string | null;
}

export interface OperatorSurfacePriorRun {
  classification: PriorRunClassification;
}

export interface OperatorSurfaceBroker {
  safety_verdict: BrokerSafetyVerdict;
  /** Independent of safety_verdict: whether the broker session is up.
   *  A paper-only account whose IBKR session has dropped is
   *  ``safety_verdict=PAPER_ONLY`` AND ``connection=DISCONNECTED``;
   *  composing them is forbidden. */
  connection: BrokerConnectionState;
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

export type GateSuggestedAction =
  | InvokeCapabilityAction
  | FocusAction
  | RedeployAction
  | OpenRunbookAction;

export interface OperatorGate {
  name: string;
  status: string;
  severity: string;
  detail: string;
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

export interface OperatorSurface {
  /** Bump on breaking shape changes; additive fields do NOT bump the version. */
  schema_version: number;
  host_process: OperatorSurfaceHostProcess;
  prior_run: OperatorSurfacePriorRun;
  broker: OperatorSurfaceBroker;
  configuration: OperatorSurfaceConfiguration;
  current_risk: OperatorSurfaceCurrentRisk;
  daily_order_cap: OperatorSurfaceDailyOrderCap;
  action_plan: OperatorSurfaceActionPlan;
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
  reference_price: string;
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
}

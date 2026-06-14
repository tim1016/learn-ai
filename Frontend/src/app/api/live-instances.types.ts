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
  fetched_at_ms: number;
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

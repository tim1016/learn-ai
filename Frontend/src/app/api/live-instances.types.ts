// Instance-addressed operator console types (ADR 0004).
// The console's subject is the strategy instance; the current run is evidence.
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
  fetched_at_ms: number;
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

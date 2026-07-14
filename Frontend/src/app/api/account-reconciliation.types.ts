import type { AccountTruthResponse } from './broker-models';
import type { GateResult } from './live-instances.types';

export type AccountReconciliationState = 'CLEAN' | 'NOT_PROVEN';
export type AccountExposureResolution = 'flat' | 'intended' | 'accepted_override' | 'unresolved';
export type AccountConditionType =
  | 'exposure_freeze'
  | 'account_freeze'
  | 'evidence_stale'
  | 'daemon_unreachable'
  | 'evidence_missing'
  | 'exit_flatten_failed'
  | 'exit_lease_stuck'
  | 'crashed'
  | 'ended_without_status'
  | 'liveness_unproven'
  | 'repeated_unclean_start';
export type AccountCureAction =
  | 'resolve_exposure'
  | 'clear_freeze'
  | 'reconcile_now'
  | 'prove_evidence'
  | 'retire_replace';

export interface AccountReconciliationEvidenceRef {
  source: string;
  ref: string;
  detail: string | null;
}

export interface AccountReconciliationReceipt {
  schema_version: number;
  receipt_id: string;
  account_id: string;
  requested_account_id: string;
  connected_account_id: string | null;
  state: AccountReconciliationState;
  account_truth_verdict: AccountTruthResponse['final_verdict'];
  account_truth_severity: AccountTruthResponse['final_severity'];
  final_gate_result: GateResult;
  exposure_resolution: AccountExposureResolution;
  account_truth: AccountTruthResponse;
  evidence_refs: AccountReconciliationEvidenceRef[];
  generated_at_ms: number;
  account_truth_generated_at_ms: number;
  expires_at_ms: number;
  ttl_ms: number;
}

export interface AccountReconciliationAutomationPolicy {
  schema_version: number;
  account_id: string;
  enabled: boolean;
  updated_at_ms: number;
  updated_by: string;
}

export interface AccountReconciliationAutomationPolicyUpdate {
  enabled: boolean;
  updated_by?: string;
}

export interface AccountConditionOwner {
  owner_type: 'account' | 'bot';
  owner_id: string;
  label: string;
  strategy_instance_id: string | null;
  run_id: string | null;
  lifecycle_state: string | null;
}

export interface AccountConditionRow {
  condition_type: AccountConditionType;
  scope: 'account' | 'bot';
  owner: AccountConditionOwner;
  severity: 'warning' | 'critical';
  title: string;
  detail: string;
  operator_next_step: string | null;
  source: string;
  evidence_at_ms: number;
  evidence_refs: AccountReconciliationEvidenceRef[];
  affected_strategy_instance_ids: string[];
  cure_action: AccountCureAction;
}

export interface AccountFreezeBanner {
  headline: string;
  detail: string;
}

export interface AccountObservationHistoryEvent {
  state: 'VERIFIED' | 'REVOKED';
  reason_line: string;
  recorded_at_ms: number;
}

export interface AccountObservationView {
  state: 'VERIFIED' | 'REVOKED' | 'EXPIRED' | 'ABSENT';
  reason_line: string;
  observed_at_ms: number | null;
  valid_until_ms: number | null;
  history: AccountObservationHistoryEvent[];
}

export interface AccountTriageBotRef {
  strategy_instance_id: string;
  run_id: string;
  bot_order_namespace: string;
  lifecycle_state: string;
}

export interface AccountTriageGateRow {
  gate_id: string;
  status: 'pass' | 'block' | 'freeze' | 'unknown';
  scope: 'account' | 'reconciliation';
  severity: 'ok' | 'warning' | 'critical';
  title: string;
  detail: string;
  operator_next_step: string | null;
  source: string;
  evidence_at_ms: number;
  affected_strategy_instance_ids: string[];
  evidence_refs: AccountReconciliationEvidenceRef[];
  primary_remediation: string | null;
}

export interface AccountTriageResponse {
  schema_version: number;
  generated_at_ms: number;
  account_id: string;
  strategy_instance_id: string | null;
  summary_headline: string;
  summary_detail: string;
  overall_gate_result: GateResult;
  account_reconciliation_receipt: AccountReconciliationReceipt | null;
  account_reconciliation_valid_until_ms: number | null;
  reconciliation_automation_policy: AccountReconciliationAutomationPolicy;
  account_observation: AccountObservationView;
  gate_rows: AccountTriageGateRow[];
  conditions: AccountConditionRow[];
  freeze_banner: AccountFreezeBanner | null;
  clear_freeze_actionable: boolean;
  affected_bots: AccountTriageBotRef[];
}

export interface LegacyStaleClaimCandidate {
  claim_id: string;
  strategy_instance_id: string;
  run_id: string;
  bot_order_namespace: string;
  symbol: string;
  claimed_quantity: number;
  proof_summary: string;
  proved_at_ms: number;
}

export interface LegacyStaleClaimCandidatesResponse {
  schema_version: number;
  account_id: string;
  generated_at_ms: number;
  candidates: LegacyStaleClaimCandidate[];
}

export interface LegacyStaleClaimRetireRequest {
  strategy_instance_id: string;
  run_id: string;
  symbol: string;
  requested_by?: string;
}

export interface LegacyStaleClaimRetirementReceipt {
  schema_version: number;
  receipt_id: string;
  account_id: string;
  strategy_instance_id: string;
  run_id: string;
  bot_order_namespace: string;
  symbol: string;
  claimed_quantity: number;
  requested_by: string;
  retired_at_ms: number;
}

export interface AccountClearFreezeRequest {
  requested_by?: string;
  receipt_id?: string | null;
  reason?: string | null;
}

export interface AccountClearFreezeResponse {
  schema_version: number;
  account_id: string;
  cleared: boolean;
  cleared_source: 'account_recovery_proof';
  recovery_id: string;
  receipt_id: string;
  gate_result: GateResult;
  triage: AccountTriageResponse;
}

export interface AccountAcceptExposureOverrideRequest {
  requested_by?: string;
  reason: string;
  strategy_instance_id?: string | null;
  run_id?: string | null;
  bot_order_namespace?: string | null;
}

export interface AccountAcceptExposureOverrideResponse {
  schema_version: number;
  account_id: string;
  cleared: boolean;
  cleared_source: 'account_audited_override';
  override_id: string;
  triage: AccountTriageResponse;
}

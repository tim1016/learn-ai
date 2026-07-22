/** Read-only roster and Account-service projections owned by the backend. */

import type { AccountTriageVerdictState } from './account-reconciliation.types';

export type AccountEffectivePosture = 'PAPER_EXECUTION' | 'UNSAFE' | 'UNKNOWN';
export type AccountServiceAttachment = 'ATTACHED' | 'UNATTACHED' | 'FENCED';
export type AccountServicePhase = 'accepting' | 'reconnecting' | 'draining' | 'frozen';
export type AccountServiceOperatingState = 'READY' | 'STANDBY' | 'ATTENTION';
export type AccountBindingLedgerReadAuthority = 'legacy_registry' | 'clerk_ledger';
export type AccountBindingLedgerParityState = 'clean' | 'dirty';
export type AccountGatePromotionState =
  | 'SAFE_DEFAULT'
  | 'WAITING_FOR_SHADOW_PARITY'
  | 'WAITING_FOR_CLERK_RESTART_SMOKE'
  | 'CLERK_PROOF_ACTIVE';

export interface AccountServiceSummary {
  readonly attachment: AccountServiceAttachment;
  readonly phase: AccountServicePhase | null;
  readonly generation: number | null;
  readonly operating_state: AccountServiceOperatingState;
  readonly headline: string;
}

export interface AccountRosterVerdictSummary {
  readonly state: AccountTriageVerdictState;
  readonly headline: string;
  readonly generated_at_ms: number;
}

export interface AccountRosterRow {
  readonly account_id: string;
  readonly broker: 'IBKR';
  readonly effective_posture: AccountEffectivePosture;
  readonly service: AccountServiceSummary;
  readonly latest_verdict_summary: AccountRosterVerdictSummary;
  readonly last_verified_at_ms: number | null;
}

export interface AccountsRosterResponse {
  readonly schema_version: 2;
  readonly rows: readonly AccountRosterRow[];
}

export interface AccountServiceBinding {
  readonly state: AccountServiceAttachment;
  readonly generation: number | null;
  readonly lease_generation: number | null;
  readonly pending_retirement_proposals: number;
  readonly ledger_read_authority: AccountBindingLedgerReadAuthority;
  readonly ledger_parity: AccountBindingLedgerParityState;
  readonly ledger_parity_issue_count: number;
}

export interface AccountServiceLease {
  readonly status: 'RUNNING' | 'DRAINING';
  readonly generation: number;
  readonly started_at_ms: number;
  readonly renewed_at_ms: number;
  readonly valid_until_ms: number;
}

export interface AccountServiceJournalWatermark {
  readonly last_seq: number | null;
  readonly last_write_ms: number | null;
}

export interface AccountServiceGateResult {
  readonly gate_id: string;
  readonly status: 'pass' | 'block' | 'freeze';
  readonly source: string;
  readonly operator_reason: string;
  readonly operator_next_step: string;
  readonly evidence_at_ms: number | null;
}

export interface AccountServiceGateAuthority {
  readonly requested_authority: 'account_truth' | 'observation_lease';
  readonly effective_authority: 'account_truth' | 'observation_lease';
  readonly promotion_state: AccountGatePromotionState;
  readonly reason_code: string;
  readonly disposition: string | null;
  readonly action_authority: 'account_truth' | 'observation_lease';
  readonly action_gate: AccountServiceGateResult;
  readonly observed_session_dates: readonly string[];
  readonly lease_weaker_comparison_count: number;
  readonly restart_smoke_recorded_at_ms: number | null;
}

export interface AccountServiceSessionPolicy {
  readonly allow_outside_live_session: boolean;
  readonly gate_result: AccountServiceGateResult;
}

export interface AccountServiceStatusResponse {
  readonly schema_version: 3;
  readonly account_id: string;
  readonly attachment: AccountServiceAttachment;
  readonly phase: AccountServicePhase | null;
  readonly generation: number | null;
  readonly generation_recorded_at_ms: number | null;
  readonly source: string | null;
  readonly binding: AccountServiceBinding;
  readonly gate_authority: AccountServiceGateAuthority;
  readonly session_policy: AccountServiceSessionPolicy;
  readonly lease: AccountServiceLease | null;
  readonly journal: AccountServiceJournalWatermark;
  readonly operating_state: AccountServiceOperatingState;
  readonly headline: string;
  readonly detail: string;
}

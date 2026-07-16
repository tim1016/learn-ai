/** Read-only roster and Account-service projections owned by the backend. */

import type { AccountTriageVerdictState } from './account-reconciliation.types';

export type AccountEffectivePosture = 'PAPER_EXECUTION' | 'UNSAFE' | 'UNKNOWN';
export type AccountServiceAttachment = 'ATTACHED' | 'UNATTACHED' | 'FENCED';
export type AccountServicePhase = 'accepting' | 'reconnecting' | 'draining' | 'frozen';

export interface AccountServiceSummary {
  readonly attachment: AccountServiceAttachment;
  readonly phase: AccountServicePhase | null;
  readonly generation: number | null;
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
  readonly schema_version: 1;
  readonly rows: readonly AccountRosterRow[];
}

export interface AccountServiceBinding {
  readonly state: AccountServiceAttachment;
  readonly generation: number | null;
  readonly lease_generation: number | null;
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

export interface AccountServiceStatusResponse {
  readonly schema_version: 1;
  readonly account_id: string;
  readonly attachment: AccountServiceAttachment;
  readonly phase: AccountServicePhase | null;
  readonly generation: number | null;
  readonly generation_recorded_at_ms: number | null;
  readonly source: string | null;
  readonly binding: AccountServiceBinding;
  readonly lease: AccountServiceLease | null;
  readonly journal: AccountServiceJournalWatermark;
}

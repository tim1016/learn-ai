/** Backend-authored account cockpit posture and daemon-supervised restore contract. */

import type { AccountServiceStatusResponse } from './account-directory.types';
import type { OperatorBlocker } from './operator-blocker.types';

export type AccountCockpitMode = 'NORMAL' | 'CLERK_DOWN' | 'JOURNAL_CORRUPT' | 'JOURNAL_EVIDENCE_HOLD' | 'DAEMON_DOWN' | 'DAEMON_UNREADABLE';
export type AccountCockpitDaemonAvailability = 'AVAILABLE' | 'DOWN' | 'UNREADABLE';

export interface AccountCockpitDaemon {
  readonly availability: AccountCockpitDaemonAvailability;
  readonly reason_code: string;
  readonly detail: string;
  readonly observed_at_ms: number;
}

export interface AccountCockpitResponse {
  readonly schema_version: 1;
  readonly account_id: string;
  readonly generated_at_ms: number;
  readonly mode: AccountCockpitMode;
  readonly clerk: AccountServiceStatusResponse;
  readonly daemon: AccountCockpitDaemon;
  readonly blockers: readonly OperatorBlocker[];
}

export interface AccountClerkRestoreRequest {
  readonly confirmation_token: 'RESTORE';
  readonly idempotency_key: string;
}

export interface AccountClerkRestoreReceipt {
  readonly schema_version: 1;
  readonly receipt_id: string;
  readonly account_id: string;
  readonly clerk_generation: number;
  readonly recorded_at_ms: number;
}

export interface JournalRecoveryRequest {
  readonly confirmation_token: 'QUARANTINE' | 'REBASELINE';
  readonly idempotency_key: string;
}

export interface JournalRecoveryReceipt {
  readonly receipt_id: string;
  readonly account_id: string;
  readonly phase: 'REBASELINE_REQUIRED' | 'COMPLETE';
  readonly recorded_at_ms: number;
  readonly quarantined_journal_name: string | null;
  readonly broker_evidence_positions: readonly { readonly symbol: string; readonly signed_quantity: number }[];
}

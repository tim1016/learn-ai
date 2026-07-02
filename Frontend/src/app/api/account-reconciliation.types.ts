import type { AccountTruthResponse } from './broker-models';
import type { GateResult } from './live-instances.types';

export type AccountReconciliationState = 'CLEAN' | 'NOT_PROVEN';

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
  account_truth: AccountTruthResponse;
  evidence_refs: AccountReconciliationEvidenceRef[];
  generated_at_ms: number;
  account_truth_generated_at_ms: number;
  expires_at_ms: number;
  ttl_ms: number;
}

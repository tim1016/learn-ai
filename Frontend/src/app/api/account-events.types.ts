export type AccountEventView = 'trader_today' | 'operations';
export type AccountEventKind =
  | 'activity'
  | 'safety'
  | 'reconciliation'
  | 'clerk'
  | 'configuration'
  | 'other';

export interface AccountEventEvidenceRef {
  source: string;
  ref: string;
  detail: string | null;
}

/** Versioned server-owned narration and classification for one journal event. */
export interface AccountEventRow {
  schema_version: 1;
  event_id: string;
  seq: number;
  kind: AccountEventKind;
  occurred_at_ms: number;
  trader_narration: string | null;
  operator_detail: string;
  evidence_refs: AccountEventEvidenceRef[];
}

export interface AccountEventsResponse {
  schema_version: 1;
  account_id: string;
  view: AccountEventView;
  rows: AccountEventRow[];
  latest_seq: number | null;
  next_before_seq: number | null;
}

export interface AccountEventsRequest {
  view: AccountEventView;
  limit?: number;
  kinds?: readonly AccountEventKind[];
  beforeSeq?: number;
  afterSeq?: number;
}

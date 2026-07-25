export type AccountEventView = 'operations';
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

/** Bounded receipt facts intentionally shown only in the operator lens. */
export interface AccountEventOperatorOrderReceipt {
  broker: 'ibkr';
  order_id: number;
  perm_id: number | null;
  order_ref: string;
  symbol: string;
  action: 'BUY' | 'SELL';
  quantity: number;
  order_type: 'MKT' | 'LMT';
  limit_price: number | null;
  status: string;
  acknowledged_at_ms: number;
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
  operator_order_receipt?: AccountEventOperatorOrderReceipt | null;
}

export interface AccountEventsResponse {
  schema_version: 1;
  account_id: string;
  view: AccountEventView;
  rows: AccountEventRow[];
  latest_seq: number | null;
  next_before_seq: number | null;
}

/** Trader-safe server outcome. This shape intentionally cannot hold a receipt. */
export interface TraderAccountEventRow {
  schema_version: 1;
  event_id: string;
  seq: number;
  occurred_at_ms: number;
  outcome: string;
}

export interface TraderAccountEventsResponse {
  schema_version: 1;
  account_id: string;
  rows: TraderAccountEventRow[];
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

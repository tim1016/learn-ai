/** Backend-authored Clerk-native transaction history contract (#1219). */

export interface ClerkTransactionEvent {
  readonly event_id: string;
  readonly event_kind: string;
  readonly journal_seq: number;
  readonly recorded_at_ms: number;
  readonly receipt: Record<string, unknown>;
}

export interface ClerkTransaction {
  readonly transaction_id: string;
  readonly account_id: string;
  readonly journal_seq: number;
  readonly recorded_at_ms: number;
  readonly transaction_kind: string;
  readonly strategy_instance_id: string;
  readonly run_id: string;
  readonly intent_id: string;
  readonly order_ref: string;
  readonly order_id: number | null;
  readonly perm_id: number | null;
  readonly exec_id: string | null;
  readonly receipt: Record<string, unknown>;
  readonly events: readonly ClerkTransactionEvent[];
}

export interface ClerkTransactionHistoryResponse {
  readonly projection_available: boolean;
  readonly canonical_fallback_required: boolean;
  readonly high_water_journal_seq: number | null;
  readonly lag_records: number | null;
  readonly rows: readonly ClerkTransaction[];
  readonly next_cursor: string | null;
}

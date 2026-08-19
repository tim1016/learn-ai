/** Backend-authored Clerk-native transaction history contract (#1219). */

export type ClerkTransactionOrigin =
  | 'manual'
  | 'strategy'
  | 'external'
  | 'unknown'
  | 'recovery'
  | 'emergency'
  | 'shutdown'
  | 'force_flat'
  | 'other';

/** SQLite-owned execution-fee provenance; the browser only renders the receipt value. */
export type ClerkFeeFidelity = 'reported' | 'not_reported';

export interface ClerkOrderInstruction {
  readonly symbol: string | null;
  readonly sec_type: string | null;
  readonly action: string | null;
  readonly quantity: number | null;
  readonly order_type: string | null;
  readonly limit_price: number | null;
  readonly stop_price: number | null;
  readonly time_in_force: string | null;
  readonly outside_rth: boolean | null;
}

export interface ClerkTransactionFilters {
  /** Explicit compatibility lineage; omission selects the Alpaca SQLite authority. */
  readonly broker?: 'alpaca' | 'ibkr';
  readonly origin?: ClerkTransactionOrigin | null;
  readonly lifecycleState?: string | null;
  readonly strategyInstanceId?: string | null;
  readonly runId?: string | null;
  /** Inclusive UTC-millisecond bounds owned by the requesting desk scope. */
  readonly fromMs?: number | null;
  readonly toMs?: number | null;
}

export interface ClerkTransactionEvent {
  readonly event_id: string;
  readonly broker: 'ibkr' | 'alpaca';
  readonly event_kind: string;
  readonly journal_seq: number;
  readonly recorded_at_ms: number;
  readonly receipt: Record<string, unknown>;
  readonly callback_identity: string;
  readonly lifecycle_state: string;
  readonly native_order_id: string | null;
  readonly native_execution_id: string | null;
  readonly commission_status: 'unknown' | 'reported';
  readonly fee: number | null;
  readonly fee_fidelity?: ClerkFeeFidelity | null;
  readonly execution_quantity?: number | null;
  readonly execution_price?: number | null;
}

/** Server-folded custody stages for the bounded evidence window in this response. */
export interface ClerkCustodyWindowSummary {
  readonly record_count: number;
  readonly a0_custody_accepted_count: number;
  readonly a1_broker_write_started_count: number;
  readonly a2_broker_known_count: number;
  readonly a3_economic_terminal_count: number;
  /** Rows whose server lifecycle cannot prove one custody stage. */
  readonly uncertain_count: number;
}

export interface ClerkCustodyDurations {
  readonly request_to_intake_ms: number | null;
  readonly intake_to_a0_ms: number | null;
  readonly a0_to_broker_write_ms: number | null;
  readonly broker_write_to_return_ms: number | null;
  readonly broker_return_to_first_callback_ms: number | null;
  readonly terminal_age_ms: number | null;
}

/** Event, arrival, and durable-record clocks for one selected receipt. */
export interface ClerkCustodyTimeline {
  readonly intent_created_at_ms: number | null;
  readonly clerk_request_received_at_ms: number | null;
  readonly clerk_intake_admitted_at_ms: number | null;
  readonly inbox_fsynced_at_ms: number | null;
  readonly a0_custody_accepted_at_ms: number | null;
  readonly broker_write_started_at_ms: number | null;
  readonly broker_call_returned_at_ms: number | null;
  readonly broker_ack_recorded_at_ms: number | null;
  readonly earliest_broker_source_at_ms: number | null;
  readonly first_callback_arrived_at_ms: number | null;
  readonly first_callback_recorded_at_ms: number | null;
  readonly economic_terminal_recorded_at_ms: number | null;
  readonly durations: ClerkCustodyDurations;
}

export interface ClerkTransactionSummary {
  readonly transaction_id: string;
  /** Broker-owned lifecycle vocabulary; the client only renders this value. */
  readonly broker: 'ibkr' | 'alpaca';
  readonly account_id: string;
  readonly journal_seq: number;
  readonly recorded_at_ms: number;
  readonly transaction_kind: string;
  readonly transaction_origin?: ClerkTransactionOrigin;
  readonly order_instruction?: ClerkOrderInstruction;
  /** Immutable custody identity, including manual operator subjects. */
  readonly subject_id?: string | null;
  /** Null for external orders that have no Clerk-owned bot identity. */
  readonly strategy_instance_id: string | null;
  readonly run_id: string | null;
  readonly intent_id: string | null;
  readonly order_ref: string | null;
  readonly order_id: number | null;
  readonly perm_id: number | null;
  readonly exec_id: string | null;
  readonly native_order_id: string | null;
  readonly native_execution_id: string | null;
  readonly lifecycle_state: string;
  readonly commission_status: 'unknown' | 'reported';
  readonly fee: number | null;
  /** Execution facts are projected by SQLite; the browser only renders them. */
  readonly execution_quantity?: number | null;
  readonly execution_price?: number | null;
  readonly fee_fidelity?: ClerkFeeFidelity | null;
  /** Durable identifier for an externally observed Alpaca order. */
  readonly external_order_id?: string | null;
  readonly event_count: number;
}

/** Full receipt evidence is fetched only after an operator selects a grid row. */
export interface ClerkTransactionDetail extends Omit<ClerkTransactionSummary, 'event_count'> {
  readonly receipt: Record<string, unknown>;
  readonly events: readonly ClerkTransactionEvent[];
  readonly custody_timeline: ClerkCustodyTimeline | null;
}

export interface ClerkTransactionHistoryResponse {
  readonly projection_available: boolean;
  readonly canonical_fallback_required: boolean;
  /** Backend-authored state; UI must not infer lifecycle freshness. */
  readonly feed_state: 'live' | 'reconnecting' | 'rebuilding' | 'stale' | 'offline_but_saved' | 'corrupt' | 'projection_unavailable';
  readonly feed_headline: string;
  readonly feed_detail: string;
  readonly high_water_journal_seq: number | null;
  readonly lag_records: number | null;
  /** True only when the bounded replay knows the lag is at least this many rows. */
  readonly lag_is_lower_bound: boolean;
  readonly custody_summary: ClerkCustodyWindowSummary;
  readonly rows: readonly ClerkTransactionSummary[];
  readonly next_cursor: string | null;
}

/** Backend receipt for one operator-reviewed external broker order. */
export interface ExternalOrderAcknowledgement {
  readonly external_order_id: string;
  readonly acknowledged_at_ms: number;
  readonly ack_operator: string;
}

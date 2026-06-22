/**
 * TypeScript wire types for the broker-activity SSE + REST surface
 * (ADR 0014).
 *
 * Hand-authored to mirror ``PythonDataService/app/schemas/broker_activity.py``
 * — kept in lockstep with that file. The frontend is render-only: every
 * operator-facing string (``headline``, ``narrative``) is authored by a
 * versioned backend template; the frontend does NOT compose, derive, or
 * classify any of it.
 *
 * All timestamps are ``int64 ms UTC`` per the numerical-rigor invariant.
 */

export type OrderSide = 'BUY' | 'SELL';

/** Closed four-value verdict enum — must NOT be derived frontend-side. */
export type Verdict =
  | 'expected'
  | 'expected_with_caveat'
  | 'unexpected'
  | 'engine_only_pending';

/** Closed reason-code vocabulary driving template selection backend-side. */
export type ReasonCode =
  | 'normal_fill'
  | 'pending_acknowledgement'
  | 'partial_fill'
  | 'timing_caveat'
  | 'reconnect_recovery'
  | 'missing_commission'
  | 'price_divergence'
  | 'quantity_divergence'
  | 'unmatched_execution'
  | 'duplicate_execution'
  | 'cancellation'
  | 'rejection';

export interface LagBreakdown {
  intent_to_dispatch_ms: number | null;
  dispatch_to_ack_ms: number | null;
  ack_to_exec_ms: number | null;
  exec_to_observed_ms: number | null;
  /** Operator-facing chip number; backend stores it so frontend never computes. */
  intent_to_exec_ms: number | null;
}

export interface SizingProvenance {
  policy: string | null;
  requested_qty: number | null;
  reference_price_decimal_str: string | null;
  provenance: string | null;
  surface: string | null;
  skip_reason: string | null;
}

export interface EngineOverlay {
  intent_id: string | null;
  mutation_attempt_id: string | null;
  requested_qty: number | null;
  requested_price: number | null;
  sizing_provenance: SizingProvenance | null;
  lag_breakdown: LagBreakdown;
}

export interface DivergenceFacts {
  price_delta: number | null;
  quantity_delta: number | null;
  lag_total_ms: number | null;
  window_context: Record<string, number | string>;
}

export interface BrokerActivityRow {
  // WAL identity
  seq: number;
  ts_ms: number;

  // Broker-recognisable columns (CP Trades mirror); null for engine_only_pending
  exec_id: string | null;
  perm_id: number | null;
  order_ref: string | null;
  symbol: string;
  side: OrderSide;
  quantity: number;
  price: number | null;
  commission: number | null;
  net_amount: number | null;
  order_type: string;
  exec_ts_ms: number | null;

  // Authored output (frozen at write time on the backend)
  verdict: Verdict;
  template_key: string;
  template_version: number;
  headline: string;
  narrative: string;
  reason_codes: ReasonCode[];

  // Drill-down structured facts
  engine_overlay: EngineOverlay | null;
  divergence_facts: DivergenceFacts | null;
}

export interface BrokerActivityPage {
  rows: BrokerActivityRow[];
  /** ``null`` when the page drained the WAL — caller switches to SSE. */
  next_seq: number | null;
}

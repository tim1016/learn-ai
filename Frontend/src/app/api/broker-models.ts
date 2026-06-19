/**
 * Broker API model aliases and SSE payload types.
 *
 * The REST schemas live in ``broker.types.ts`` (regenerated from the
 * Python service's OpenAPI spec — see ``Frontend/AGENTS.md``). SSE
 * endpoints emit ``text/event-stream`` so FastAPI does not surface
 * their payload shape via OpenAPI; we mirror those Pydantic models
 * here by hand to keep one source of typed truth in the frontend.
 *
 * If you change a Pydantic model under ``app.broker.ibkr.models``,
 * either regenerate the REST file (REST-shaped models) or update this
 * file (SSE payloads).
 */

import type { components } from './broker.types';

// ── REST-shaped models (sourced from OpenAPI) ─────────────────────────

export type IbkrAccountSummary = components['schemas']['IbkrAccountSummary'];
export type IbkrConnectionHealth = components['schemas']['IbkrConnectionHealth'];
export type IbkrOpenOrder = components['schemas']['IbkrOpenOrder'];
export type IbkrOrderAck = components['schemas']['IbkrOrderAck'];
export type IbkrOrderSpec = components['schemas']['IbkrOrderSpec'];
export type IbkrPosition = components['schemas']['IbkrPosition'];
export type IbkrPositionsSnapshot = components['schemas']['IbkrPositionsSnapshot'];

export type OptionRight = 'C' | 'P';
export type OrderAction = 'BUY' | 'SELL';
export type OrderType = 'MKT' | 'LMT';
export type OrderTimeInForce = 'DAY' | 'GTC' | 'IOC' | 'OPG';
export type OrderStatus =
  | 'PendingSubmit'
  | 'PendingCancel'
  | 'PreSubmitted'
  | 'Submitted'
  | 'ApiPending'
  | 'ApiCancelled'
  | 'Cancelled'
  | 'Filled'
  | 'Inactive'
  | 'Unknown';
export type SecType =
  | 'STK'
  | 'OPT'
  | 'FUT'
  | 'FOP'
  | 'CASH'
  | 'BOND'
  | 'CFD'
  | 'WAR'
  | 'IND'
  | 'BAG';
export type GreeksSource = 'model' | 'bid' | 'ask' | 'last' | 'none';
export type OrderEventType = 'status' | 'fill' | 'cancel' | 'error';

// ── SSE payload models (hand-mirrored from app.broker.ibkr.models) ────

export interface IbkrOptionQuote {
  symbol: string;
  expiry_ms: number;
  strike: number;
  right: OptionRight;
  bid: number | null;
  ask: number | null;
  last: number | null;
  bid_size: number | null;
  ask_size: number | null;
  iv: number | null;
  delta: number | null;
  gamma: number | null;
  theta: number | null;
  vega: number | null;
  underlying_price: number | null;
  greeks_source: GreeksSource;
  ts_ms: number;
}

export interface IbkrChainSnapshot {
  symbol: string;
  expiry_ms: number;
  underlying_price: number | null;
  quotes: IbkrOptionQuote[];
  as_of_ms: number;
}

export interface IbkrSurfaceExpiry {
  expiry_ms: number;
  quotes: IbkrOptionQuote[];
}

export interface IbkrSurfaceSnapshot {
  symbol: string;
  underlying_price: number | null;
  expiries: IbkrSurfaceExpiry[];
  /** Total IBKR streaming market-data lines this surface holds open. */
  line_count: number;
  as_of_ms: number;
}

export interface IbkrPnLTick {
  account_id: string;
  con_id: number | null;
  daily_pnl: number | null;
  unrealized_pnl: number | null;
  realized_pnl: number | null;
  market_value: number | null;
  position: number | null;
  ts_ms: number;
}

export interface IbkrOrderEvent {
  account_id: string;
  order_id: number;
  perm_id: number | null;
  con_id: number | null;
  event_type: OrderEventType;
  status: OrderStatus | null;
  fill_quantity: number | null;
  avg_fill_price: number | null;
  cumulative_filled: number | null;
  remaining: number | null;
  last_fill_price: number | null;
  error_code: number | null;
  error_message: string | null;
  ts_ms: number;
}

// ── REST shape: /api/broker/diagnose ─────────────────────────────────
// Hand-mirrored from app.broker.ibkr.models.DiagnosticCheck /
// DiagnosticReport. Regenerate broker.types.ts when this file is next
// regenerated to retire the hand mirror.

export type DiagnosticStatus = 'pass' | 'warn' | 'fail' | 'skip';

export interface DiagnosticCheck {
  name: string;
  label: string;
  status: DiagnosticStatus;
  detail: string;
  fix: string | null;
}

export interface DiagnosticReportActive {
  disabled: false;
  overall_status: 'pass' | 'warn' | 'fail';
  checks: DiagnosticCheck[];
  fetched_at_ms: number;
}

export interface DiagnosticReportDisabled {
  disabled: true;
  reason: string;
  since_ms: number;
}

export type DiagnosticReport = DiagnosticReportActive | DiagnosticReportDisabled;

// ── REST shape: /api/broker/expirations/{symbol} ─────────────────────

export interface ExpirationsResponse {
  symbol: string;
  expirations_ms: number[];
}

// ── REST shape: /api/broker/strikes/{symbol} ─────────────────────────

export interface IbkrStrikeList {
  symbol: string;
  expiry_ms: number;
  strikes: number[];
  fetched_at_ms: number;
}

// ── REST shape: /api/broker/symbols/search (Slice 1F) ────────────────

export interface SymbolMatch {
  symbol: string;
  name: string;
  exchange: string;
  currency: string;
  sec_type: 'STK' | 'OPT' | 'FUT' | 'FOP' | 'IND' | 'CASH' | 'BOND' | 'CFD' | 'CMDTY';
  derivative_sec_types: string[];
}

export interface SymbolSearchResponse {
  matches: SymbolMatch[];
}

// ── REST shape: /api/broker/option-contracts/{symbol} (Slice 1F) ─────

export interface OptionContractMatch {
  con_id: number;
  symbol: string;
  local_symbol: string;
  trading_class: string;
  exchange: string;
  currency: string;
  expiry_ms: number;
  strike: number;
  right: 'C' | 'P';
  multiplier: number;
}

export interface OptionContractsResponse {
  matches: OptionContractMatch[];
}

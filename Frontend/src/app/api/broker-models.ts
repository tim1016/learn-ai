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

export type IbkrConnectionHealth = components['schemas']['IbkrConnectionHealth'];
export type PanelActionErrorResponse = components['schemas']['PanelActionErrorResponse'];

export type OptionRight = 'C' | 'P';
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
export type IbkrApiRequestName =
  | 'accountSummaryAsync'
  | 'cancelMktData'
  | 'placeOrder'
  | 'cancelOrder'
  | 'qualifyContractsAsync'
  | 'reqAllOpenOrders'
  | 'reqCompletedOrdersAsync'
  | 'reqContractDetailsAsync'
  | 'reqCurrentTimeAsync'
  | 'reqExecutionsAsync'
  | 'reqMatchingSymbolsAsync'
  | 'reqMktData'
  | 'reqMarketDataType'
  | 'reqPnL'
  | 'reqPnLSingle'
  | 'reqPositionsAsync'
  | 'reqRealTimeBars'
  | 'reqSecDefOptParamsAsync'
  | 'whatIfOrderAsync';
export type IbkrApiCallbackName =
  | 'accountSummary'
  | 'completedOrder'
  | 'contractDetails'
  | 'currentTime'
  | 'error'
  | 'marketDataType'
  | 'openOrder'
  | 'orderStatus'
  | 'execDetails'
  | 'pnl'
  | 'pnlSingle'
  | 'position'
  | 'realTimeBar'
  | 'realTimeBarList'
  | 'securityDefinitionOptionParameter'
  | 'symbolSamples'
  | 'tickSnapshot'
  | 'whatIfOrder';
export type IbkrEvidenceScalar = string | number | boolean | null;
export type IbkrEvidenceValue =
  | IbkrEvidenceScalar
  | IbkrEvidenceValue[]
  | { [key: string]: IbkrEvidenceValue };

export interface IbkrApiRequestEvidence {
  call: IbkrApiRequestName;
  params: Record<string, IbkrEvidenceValue>;
}

export interface IbkrSerializerWarning {
  object_type: string;
  serializer_error: string;
}

export interface IbkrApiResponseEvidence {
  callback: IbkrApiCallbackName;
  fields: Record<string, IbkrEvidenceValue>;
  serializer_warnings: IbkrSerializerWarning[];
}

export interface IbkrApiEvidenceEvent {
  seq: number;
  ts_ms: number;
  source: string;
  account_id: string | null;
  symbol: string | null;
  strategy_instance_id: string | null;
  request: IbkrApiRequestEvidence;
  response: IbkrApiResponseEvidence | null;
  error: string | null;
}

export type DataPlaneHealth = components['schemas']['DataPlaneHealth'];

export type SessionKind = 'RTH' | 'PRE' | 'POST' | 'OVERNIGHT';
export type CapabilityDataQuality =
  | 'live'
  | 'delayed'
  | 'frozen'
  | 'delayed_frozen'
  | 'none';
export type CapabilityTradeability = 'yes' | 'needs_enablement' | 'no';
export type CapabilityAccountMode = 'live' | 'paper';

export interface SessionCapability {
  window_today_open_ms: number | null;
  window_today_close_ms: number | null;
  data: CapabilityDataQuality;
  tradeable: CapabilityTradeability;
  order_eligible_outside_rth: boolean;
  evidence_codes: number[];
}

export interface SessionDataCapability {
  symbol: string;
  con_id: number;
  account_mode: CapabilityAccountMode;
  account_id: string;
  probed_at_ms: number;
  time_zone_id: string;
  sessions: Record<SessionKind, SessionCapability>;
  raw_evidence: IbkrApiEvidenceEvent[];
}

export interface BrokerCapabilityResponse {
  snapshots: SessionDataCapability[];
}

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

/**
 * View models of the market-data and pricing screens.
 *
 * Python answers in snake_case (`components['schemas']` of the generated
 * contract); the screens read these camelCase shapes. Where the two are the
 * same shape a type here is an alias of the generated one. Where only the
 * case differs, `market-data.mappers.ts` holds the one small mapper that turns
 * the wire type into the view model. Every timestamp stays `int64 ms UTC`.
 */

import type { components } from '../api/broker.types';

type Schema = components['schemas'];

// ------------------------------------------------------------------
// Aggregate bars
// ------------------------------------------------------------------

/** One OHLCV bar, the wire shape as-is: `timestamp` is the bar's `int64 ms UTC`. */
export type StockAggregate = Schema['AggregateBar'];

export interface StockAggregatesResult {
  /** The ticker as asked, upper-cased. */
  ticker: string;
  aggregates: StockAggregate[];
}

// ------------------------------------------------------------------
// Stock snapshot (single ticker)
// ------------------------------------------------------------------

export type SnapshotBar = Schema['SnapshotBar'];

export interface MinuteBar extends SnapshotBar {
  accumulatedVolume: number | null;
  timestamp: number | null;
}

export interface StockTickerSnapshot {
  ticker: string | null;
  day: SnapshotBar | null;
  prevDay: SnapshotBar | null;
  min: MinuteBar | null;
  todaysChange: number | null;
  todaysChangePercent: number | null;
  updated: number | null;
}

export interface StockSnapshotResult {
  success: boolean;
  snapshot: StockTickerSnapshot | null;
  error: string | null;
}

// ------------------------------------------------------------------
// Options chain snapshot
// ------------------------------------------------------------------

export type GreeksSnapshot = Schema['GreeksSnapshot'];

export type DaySnapshot = Schema['DaySnapshot'];

export type LastTradeSnapshot = Pick<Schema['LastTradeSnapshot'], 'price' | 'size' | 'exchange' | 'timeframe'>;

export interface LastQuoteSnapshot {
  bid: number | null;
  ask: number | null;
  bidSize: number | null;
  askSize: number | null;
  midpoint: number | null;
  timeframe: string | null;
}

export interface SnapshotUnderlyingResult {
  ticker: string;
  price: number;
  change: number;
  changePercent: number;
}

export interface SnapshotContractResult {
  ticker: string | null;
  contractType: string | null;
  strikePrice: number | null;
  /** The expiry anchor instant: 16:00 New York time on the expiry date. */
  expirationMs: number | null;
  breakEvenPrice: number | null;
  impliedVolatility: number | null;
  openInterest: number | null;
  greeks: GreeksSnapshot | null;
  day: DaySnapshot | null;
  lastTrade: LastTradeSnapshot | null;
  lastQuote: LastQuoteSnapshot | null;
}

export interface OptionsChainSnapshotResult {
  success: boolean;
  underlying: SnapshotUnderlyingResult | null;
  contracts: SnapshotContractResult[];
  count: number;
  /** Annualized risk-free rate for the chain (FRED-sourced, interpolated at the expiry's tenor). */
  riskFreeRate: number | null;
  /** Continuous-dividend-yield proxy (Polygon trailing-12-month / spot). */
  dividendYield: number | null;
  rateSource: string | null;
  dividendSource: string | null;
  error: string | null;
}

// ------------------------------------------------------------------
// Options strategy analysis
// ------------------------------------------------------------------

export interface StrategyLegInput {
  legId?: string;
  strike: number;
  optionType: string;
  position: string;
  premium: number;
  iv: number;
  quantity?: number;
}

/**
 * Opt-in flags for `analyzeOptionsStrategy`. Default-false; setting
 * any of these to true causes the corresponding response field to be populated.
 */
export interface StrategyAnalyzeOptions {
  includeCurrentCurve?: boolean;
  includeGreekCurves?: boolean;
  includeLegDiagnostics?: boolean;
  whatIfTimeShiftDays?: number;
  whatIfIvShift?: number;
}

export type PayoffPoint = Schema['PayoffPoint'];

export type GreeksResult = Required<Schema['GreeksResult']>;

export interface CurrentCurvePoint {
  price: number;
  theoreticalValue: number;
  theoreticalPnl: number;
}

export type GreekCurvePointResult = Schema['GreekCurvePoint'];

export interface LegDiagnosticResult {
  legId: string | null;
  strike: number;
  optionType: string;
  position: string;
  quantity: number;
  iv: number;
  entryPremium: number;
  currentTheoretical: number;
  currentDelta: number;
  currentGamma: number;
  currentTheta: number;
  currentVega: number;
  legPnl: number;
}

export interface StrategyAnalyzeResult {
  success: boolean;
  symbol: string;
  spotPrice: number;
  strategyCost: number;
  pop: number;
  expectedValue: number;
  maxProfit: number;
  maxLoss: number;
  breakevens: number[];
  curve: PayoffPoint[];
  greeks: GreeksResult;
  // null unless the matching include_* flag was set on the request.
  currentCurve: CurrentCurvePoint[] | null;
  greekCurves: GreekCurvePointResult[] | null;
  legDiagnostics: LegDiagnosticResult[] | null;
  error: string | null;
}

// ------------------------------------------------------------------
// Pricing engine toggle
// ------------------------------------------------------------------

export type QuantLibEngine = 'analytic_bs' | 'binomial_crr' | 'binomial_jr' | 'binomial_lr' | 'finite_diff' | 'monte_carlo';

export type QuantLibPriceResult = Schema['QuantLibGreeksResponse'];

// ------------------------------------------------------------------
// Chart enhancement types
// ------------------------------------------------------------------

export type GreekType = 'delta' | 'gamma' | 'theta' | 'vega' | 'rho';

export interface WhatIfScenario {
  id: string;
  label: string;
  enabled: boolean;
  timeDeltaDays: number;
  ivShift: number;
  color: string;
}

export interface ChartCurveData {
  label: string;
  points: PayoffPoint[];
  color: string;
  borderDash?: number[];
}

export interface GreekCurvePoint {
  price: number;
  value: number;
}

// ------------------------------------------------------------------
// Pricing model comparison
// ------------------------------------------------------------------

/** `theta`, `vega` and `rho` are null when the engine does not provide them (Monte Carlo has none of the three). */
export type PricingPoint = Schema['PricingPointResult'];

export type PricingModelCurve = Schema['PricingModelCurve'];

/** A model the comparison asked for and has no curve for, with the service's reason. */
export type PricingModelUnavailable = Schema['PricingModelUnavailable'];

export interface PricingCompareParams {
  spot: number;
  strike: number;
  volatility: number;
  /** The expiry anchor instant: 16:00 New York time on the expiry date. */
  expirationMs: number;
  optionType: string;
  /** Omit to have Python price at its one default rate. */
  riskFreeRate?: number;
  dividendYield?: number;
  evaluationDate?: string;
  spotMin?: number;
  spotMax?: number;
  numPoints?: number;
}

export interface PricingCompareResult {
  success: boolean;
  strike: number;
  optionType: string;
  /** The expiry anchor instant: 16:00 New York time on the expiry date. */
  expirationMs: number;
  timeToExpiryYears: number;
  /** The rate Python priced every curve at; null when the call failed before it priced anything. */
  riskFreeRate: number | null;
  models: PricingModelCurve[];
  /** Each requested model with no curve in `models`; empty when every model ran. */
  unavailableModels: PricingModelUnavailable[];
  error: string | null;
}

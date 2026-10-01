export interface StockAggregate {
  id: number;
  tickerId: number;
  open: number;
  high: number;
  low: number;
  close: number;
  volume: number;
  volumeWeightedAveragePrice: number | null;
  timestamp: number;
  timespan: string;
  multiplier: number;
  transactionCount: number | null;
}

export interface SmartAggregatesResult {
  ticker: string;
  aggregates: StockAggregate[];
}

// Stock Snapshot types (v2 API)
export interface SnapshotBar {
  open: number | null;
  high: number | null;
  low: number | null;
  close: number | null;
  volume: number | null;
  vwap: number | null;
}

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

// Options Chain Snapshot types
export interface GreeksSnapshot {
  delta: number | null;
  gamma: number | null;
  theta: number | null;
  vega: number | null;
}

export interface DaySnapshot {
  open: number | null;
  high: number | null;
  low: number | null;
  close: number | null;
  volume: number | null;
  vwap: number | null;
}

export interface SnapshotUnderlyingResult {
  ticker: string;
  price: number;
  change: number;
  changePercent: number;
}

export interface LastTradeSnapshot {
  price: number | null;
  size: number | null;
  exchange: number | null;
  timeframe: string | null;
}

export interface LastQuoteSnapshot {
  bid: number | null;
  ask: number | null;
  bidSize: number | null;
  askSize: number | null;
  midpoint: number | null;
  timeframe: string | null;
}

export interface SnapshotContractResult {
  ticker: string | null;
  contractType: string | null;
  strikePrice: number | null;
  expirationDate: string | null;
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
  /** Annualized risk-free rate for the chain (FRED-sourced, ~30d tenor). */
  riskFreeRate: number | null;
  /** Continuous-dividend-yield proxy (Polygon trailing-12-month / spot). */
  dividendYield: number | null;
  rateSource: string | null;
  dividendSource: string | null;
  error: string | null;
}

// Options Strategy Analysis types
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

export interface PayoffPoint {
  price: number;
  pnl: number;
}

export interface GreeksResult {
  delta: number;
  gamma: number;
  theta: number;
  vega: number;
}

export interface CurrentCurvePoint {
  price: number;
  theoreticalValue: number;
  theoreticalPnl: number;
}

export interface GreekCurvePointResult {
  price: number;
  delta: number;
  gamma: number;
  theta: number;
  vega: number;
}

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
  // Phase 1.1: null unless the matching include_* flag was set on the request.
  currentCurve: CurrentCurvePoint[] | null;
  greekCurves: GreekCurvePointResult[] | null;
  legDiagnostics: LegDiagnosticResult[] | null;
  error: string | null;
}

// ------------------------------------------------------------------
// Pricing Engine Toggle
// ------------------------------------------------------------------

export type QuantLibEngine = 'analytic_bs' | 'binomial_crr' | 'binomial_jr' | 'binomial_lr' | 'finite_diff' | 'monte_carlo';

export interface QuantLibPriceResult {
  success: boolean;
  engine: string;
  price: number;
  delta: number;
  gamma: number;
  theta: number;
  vega: number;
  rho: number;
  d1: number | null;
  d2: number | null;
  error: string | null;
}

// Chart enhancement types
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
// Pricing Model Comparison
// ------------------------------------------------------------------

export interface PricingPoint {
  spot: number;
  price: number;
  delta: number;
  gamma: number;
  theta: number;
  vega: number;
  rho: number;
}

export interface PricingModelCurve {
  model: string;
  points: PricingPoint[];
}

export interface PricingCompareResult {
  success: boolean;
  strike: number;
  optionType: string;
  expirationDate: string;
  timeToExpiryYears: number;
  models: PricingModelCurve[];
  error: string | null;
}

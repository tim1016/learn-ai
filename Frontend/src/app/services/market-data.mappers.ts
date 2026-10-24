/**
 * The seam between Python's snake_case wire types (`components['schemas']`) and
 * the camelCase view models in `market-data.types.ts`: one small explicit mapper
 * per type whose names differ, request builders for the wire bodies, and the
 * failed result each in-band operation falls back to when the HTTP call fails.
 */

import type { components } from '../api/broker.types';
import type {
  CurrentCurvePoint,
  LastQuoteSnapshot,
  LegDiagnosticResult,
  MinuteBar,
  OptionsChainSnapshotResult,
  PricingCompareParams,
  PricingCompareResult,
  QuantLibPriceResult,
  SnapshotContractResult,
  SnapshotUnderlyingResult,
  StockSnapshotResult,
  StockTickerSnapshot,
  StrategyAnalyzeOptions,
  StrategyAnalyzeResult,
  StrategyLegInput,
} from './market-data.types';

type Schema = components['schemas'];

// ------------------------------------------------------------------
// Stock snapshot
// ------------------------------------------------------------------

function toMinuteBar(minute: Schema['MinuteBar']): MinuteBar {
  const { accumulated_volume: accumulatedVolume, timestamp, ...bar } = minute;
  return { ...bar, accumulatedVolume: accumulatedVolume ?? null, timestamp: timestamp ?? null };
}

function toStockTickerSnapshot(snapshot: Schema['StockTickerSnapshot']): StockTickerSnapshot {
  return {
    ticker: snapshot.ticker ?? null,
    day: snapshot.day ?? null,
    prevDay: snapshot.prev_day ?? null,
    min: snapshot.min ? toMinuteBar(snapshot.min) : null,
    todaysChange: snapshot.todays_change ?? null,
    todaysChangePercent: snapshot.todays_change_percent ?? null,
    updated: snapshot.updated ?? null,
  };
}

export function toStockSnapshotResult(response: Schema['StockSnapshotResponse']): StockSnapshotResult {
  return {
    success: response.success,
    snapshot: response.snapshot ? toStockTickerSnapshot(response.snapshot) : null,
    error: response.error ?? null,
  };
}

export const failedStockSnapshot = (error: string): StockSnapshotResult => ({ success: false, snapshot: null, error });

// ------------------------------------------------------------------
// Options chain snapshot
// ------------------------------------------------------------------

function toLastQuote(quote: Schema['LastQuoteSnapshot']): LastQuoteSnapshot {
  return {
    bid: quote.bid ?? null,
    ask: quote.ask ?? null,
    bidSize: quote.bid_size ?? null,
    askSize: quote.ask_size ?? null,
    midpoint: quote.midpoint ?? null,
    timeframe: quote.timeframe ?? null,
  };
}

function toSnapshotContract(contract: Schema['OptionsContractSnapshotItem']): SnapshotContractResult {
  return {
    ticker: contract.ticker ?? null,
    contractType: contract.contract_type ?? null,
    strikePrice: contract.strike_price ?? null,
    expirationMs: contract.expiration_ms ?? null,
    standard: contract.standard ?? null,
    breakEvenPrice: contract.break_even_price ?? null,
    impliedVolatility: contract.implied_volatility ?? null,
    openInterest: contract.open_interest ?? null,
    greeks: contract.greeks ?? null,
    day: contract.day ?? null,
    lastTrade: contract.last_trade ?? null,
    lastQuote: contract.last_quote ? toLastQuote(contract.last_quote) : null,
  };
}

/** Python reports an underlying it could not price as 0 (and so does this); null fields mean the same. */
function toSnapshotUnderlying(underlying: Schema['UnderlyingSnapshot']): SnapshotUnderlyingResult {
  return {
    ticker: underlying.ticker,
    price: underlying.price ?? 0,
    change: underlying.change ?? 0,
    changePercent: underlying.change_percent ?? 0,
  };
}

export function toOptionsChainSnapshotResult(
  response: Schema['OptionsChainSnapshotResponse'],
): OptionsChainSnapshotResult {
  const contracts = (response.contracts ?? []).map(toSnapshotContract);
  return {
    success: response.success,
    underlying: response.underlying ? toSnapshotUnderlying(response.underlying) : null,
    contracts,
    count: response.count ?? contracts.length,
    riskFreeRate: response.risk_free_rate ?? null,
    dividendYield: response.dividend_yield ?? null,
    rateSource: response.rate_source ?? null,
    dividendSource: response.dividend_source ?? null,
    error: response.error ?? null,
  };
}

export const failedOptionsChainSnapshot = (error: string): OptionsChainSnapshotResult => ({
  success: false,
  underlying: null,
  contracts: [],
  count: 0,
  riskFreeRate: null,
  dividendYield: null,
  rateSource: null,
  dividendSource: null,
  error,
});

// ------------------------------------------------------------------
// Options strategy analysis
// ------------------------------------------------------------------

function toWireLeg(leg: StrategyLegInput): Schema['StrategyLeg'] {
  // `undefined` is dropped by JSON; Python would reject a `null` quantity.
  return {
    leg_id: leg.legId,
    strike: leg.strike,
    option_type: leg.optionType,
    position: leg.position,
    premium: leg.premium,
    iv: leg.iv,
    quantity: leg.quantity,
  };
}

export function toStrategyAnalyzeRequest(
  symbol: string,
  legs: StrategyLegInput[],
  expirationMs: number,
  spotPrice: number,
  riskFreeRate: number | null,
  options: StrategyAnalyzeOptions,
): Schema['StrategyAnalyzeRequest'] {
  return {
    symbol,
    legs: legs.map(toWireLeg),
    expiration_ms: expirationMs,
    spot_price: spotPrice,
    // Python fills its one default rate when none is sent, and rejects a `null`.
    ...(riskFreeRate === null ? {} : { risk_free_rate: riskFreeRate }),
    include_current_curve: options.includeCurrentCurve ?? false,
    include_greek_curves: options.includeGreekCurves ?? false,
    include_leg_diagnostics: options.includeLegDiagnostics ?? false,
    what_if_time_shift_days: options.whatIfTimeShiftDays ?? 0,
    what_if_iv_shift: options.whatIfIvShift ?? 0,
  };
}

function toCurrentCurvePoint(point: Schema['CurrentCurvePoint']): CurrentCurvePoint {
  return { price: point.price, theoreticalValue: point.theoretical_value, theoreticalPnl: point.theoretical_pnl };
}

function toLegDiagnostic(leg: Schema['LegDiagnostic']): LegDiagnosticResult {
  return {
    legId: leg.leg_id ?? null,
    strike: leg.strike,
    optionType: leg.option_type,
    position: leg.position,
    quantity: leg.quantity,
    iv: leg.iv,
    entryPremium: leg.entry_premium,
    currentTheoretical: leg.current_theoretical,
    currentDelta: leg.current_delta,
    currentGamma: leg.current_gamma,
    currentTheta: leg.current_theta,
    currentVega: leg.current_vega,
    legPnl: leg.leg_pnl,
  };
}

export function toStrategyAnalyzeResult(response: Schema['StrategyAnalyzeResponse']): StrategyAnalyzeResult {
  return {
    success: response.success,
    symbol: response.symbol ?? '',
    spotPrice: response.spot_price ?? 0,
    strategyCost: response.strategy_cost ?? 0,
    pop: response.pop ?? 0,
    expectedValue: response.expected_value ?? 0,
    maxProfit: response.max_profit ?? 0,
    maxLoss: response.max_loss ?? 0,
    breakevens: response.breakevens ?? [],
    curve: response.curve ?? [],
    greeks: {
      delta: response.greeks?.delta ?? 0,
      gamma: response.greeks?.gamma ?? 0,
      theta: response.greeks?.theta ?? 0,
      vega: response.greeks?.vega ?? 0,
    },
    currentCurve: response.current_curve?.map(toCurrentCurvePoint) ?? null,
    greekCurves: response.greek_curves ?? null,
    legDiagnostics: response.leg_diagnostics?.map(toLegDiagnostic) ?? null,
    error: response.error ?? null,
  };
}

export const failedStrategyAnalysis = (error: string): StrategyAnalyzeResult => ({
  success: false,
  symbol: '',
  spotPrice: 0,
  strategyCost: 0,
  pop: 0,
  expectedValue: 0,
  maxProfit: 0,
  maxLoss: 0,
  breakevens: [],
  curve: [],
  greeks: { delta: 0, gamma: 0, theta: 0, vega: 0 },
  currentCurve: null,
  greekCurves: null,
  legDiagnostics: null,
  error,
});

// ------------------------------------------------------------------
// Pricing
// ------------------------------------------------------------------

export function toPricingCompareRequest(params: PricingCompareParams): Schema['PricingCompareRequest'] {
  // `undefined` is dropped by JSON; Python rejects a `null` for each of these numbers.
  return {
    spot: params.spot,
    strike: params.strike,
    volatility: params.volatility,
    expiration_ms: params.expirationMs,
    option_type: params.optionType,
    risk_free_rate: params.riskFreeRate,
    dividend_yield: params.dividendYield ?? 0,
    evaluation_date: params.evaluationDate,
    spot_min: params.spotMin,
    spot_max: params.spotMax,
    num_points: params.numPoints ?? 100,
  };
}

export function toPricingCompareResult(response: Schema['PricingCompareResponse']): PricingCompareResult {
  return {
    success: response.success,
    strike: response.strike,
    optionType: response.option_type,
    expirationMs: response.expiration_ms,
    timeToExpiryYears: response.time_to_expiry_years,
    riskFreeRate: response.risk_free_rate,
    models: response.models,
    unavailableModels: response.unavailable_models ?? [],
    error: response.error ?? null,
  };
}

/** The failed comparison for `params`, echoing them the way Python's own failed answer does. */
export const failedPricingComparison =
  (params: PricingCompareParams) =>
  (error: string): PricingCompareResult => ({
    success: false,
    strike: params.strike,
    optionType: params.optionType,
    expirationMs: params.expirationMs,
    timeToExpiryYears: 0,
    riskFreeRate: params.riskFreeRate ?? null,
    models: [],
    unavailableModels: [],
    error,
  });

/** The failed price for `engine`, zero-filled the way Python's own failed answer is. */
export const failedQuantLibPrice =
  (engine: string) =>
  (error: string): QuantLibPriceResult => ({
    success: false,
    engine,
    price: 0,
    delta: 0,
    gamma: 0,
    theta: 0,
    vega: 0,
    rho: 0,
    d1: null,
    d2: null,
    error,
  });

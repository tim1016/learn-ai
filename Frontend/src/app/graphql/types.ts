export interface Author {
  id: number;
  name: string;
  bio?: string;
  books?: Book[];
}

export interface Book {
  id: number;
  title: string;
  publishedYear: number;
  authorId: number;
  author?: Author;
}

export interface GetBooksResponse {
  books: Book[];
}

export interface GetAuthorsResponse {
  authors: Author[];
}

export interface StockAggregate {
  id: number;
  tickerId: number;
  open: number;
  high: number;
  low: number;
  close: number;
  volume: number;
  volumeWeightedAveragePrice: number | null;
  timestamp: string;
  timespan: string;
  multiplier: number;
  transactionCount: number | null;
}

export interface AggregatesSummary {
  periodHigh: number;
  periodLow: number;
  averageVolume: number;
  averageVwap: number | null;
  openPrice: number;
  closePrice: number;
  priceChange: number;
  priceChangePercent: number;
  totalBars: number;
}

export interface GapDetectionInfo {
  totalWeekdays: number;
  daysWithData: number;
  missingDays: number;
  partialDays: number;
  coveragePercent: number;
  expectedBars: number;
  actualBars: number;
  missingDates: string[];
  partialDates: string[];
}

export interface SmartAggregatesResult {
  ticker: string;
  aggregates: StockAggregate[];
  summary: AggregatesSummary | null;
  gapDetection: GapDetectionInfo | null;
}

export interface FetchProgress {
  ticker: string;
  totalWindows: number;
  completedWindows: number;
  barsFetched: number;
  currentWindow: string;
  status: string;
}

export interface GetOrFetchStockAggregatesResponse {
  getOrFetchStockAggregates: SmartAggregatesResult;
}

export interface Ticker {
  id: number;
  symbol: string;
  name: string;
  market: string;
  locale: string | null;
  primaryExchange: string | null;
  type: string | null;
  active: boolean;
  currencySymbol: string | null;
  createdAt: string;
  updatedAt: string | null;
  sanitizationSummary: string | null;
}

export interface GetTickersResponse {
  tickers: Ticker[];
}

export interface IndicatorPoint {
  timestamp: number;
  value: number | null;
  signal: number | null;
  histogram: number | null;
  upper: number | null;
  lower: number | null;
}

export interface IndicatorSeries {
  name: string;
  window: number;
  data: IndicatorPoint[];
}

export interface CalculateIndicatorsResult {
  success: boolean;
  ticker: string;
  indicators: IndicatorSeries[];
  /** Error description when success = false. null on success. */
  error: string | null;
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

export interface StockSnapshotsResult {
  success: boolean;
  snapshots: StockTickerSnapshot[];
  count: number;
  error: string | null;
}

export interface MarketMoversResult {
  success: boolean;
  tickers: StockTickerSnapshot[];
  count: number;
  error: string | null;
}

// Unified Snapshot types (v3 API)
export interface UnifiedSession {
  price: number | null;
  change: number | null;
  changePercent: number | null;
  open: number | null;
  close: number | null;
  high: number | null;
  low: number | null;
  previousClose: number | null;
  volume: number | null;
}

export interface UnifiedSnapshotItem {
  ticker: string | null;
  type: string | null;
  marketStatus: string | null;
  name: string | null;
  session: UnifiedSession | null;
}

export interface UnifiedSnapshotResult {
  success: boolean;
  results: UnifiedSnapshotItem[];
  count: number;
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

export interface OptionsContract {
  ticker: string;
  underlyingTicker: string | null;
  contractType: string | null;
  strikePrice: number | null;
  expirationDate: string | null;
  exerciseStyle: string | null;
}

export interface OptionsContractsResult {
  success: boolean;
  contracts: OptionsContract[];
  count: number;
  error: string | null;
}

// Ticker Reference types
export interface TickerInfo {
  ticker: string;
  name: string;
  market: string;
  type: string;
  active: boolean;
  primaryExchange: string | null;
  currencyName: string | null;
}

export interface TrackedTickersResult {
  success: boolean;
  tickers: TickerInfo[];
  count: number;
  error: string | null;
}

export interface TickerAddress {
  address1: string | null;
  city: string | null;
  state: string | null;
  postalCode: string | null;
}

export interface TickerDetailResult {
  success: boolean;
  ticker: string;
  name: string;
  description: string | null;
  marketCap: number | null;
  homepageUrl: string | null;
  totalEmployees: number | null;
  listDate: string | null;
  sicDescription: string | null;
  primaryExchange: string | null;
  type: string | null;
  weightedSharesOutstanding: number | null;
  address: TickerAddress | null;
  error: string | null;
}

export interface RelatedTickersResult {
  success: boolean;
  ticker: string;
  related: string[];
  error: string | null;
}

// Backtest types
export interface BacktestTrade {
  tradeType: string;
  entryTimestamp: string;
  exitTimestamp: string;
  entryPrice: number;
  exitPrice: number;
  pnl: number;
  cumulativePnl: number;
  signalReason: string;
}

export interface BacktestResult {
  success: boolean;
  id: number | null;
  strategyName: string | null;
  parameters: string | null;
  totalTrades: number;
  winningTrades: number;
  losingTrades: number;
  totalPnL: number;
  maxDrawdown: number;
  sharpeRatio: number;
  durationMs: number;
  sourceBars: number;
  rthBars: number;
  resampledBars: number;
  timeframe: string;
  trades: BacktestTrade[];
  error: string | null;
}

// Options Strategy Analysis types
export interface StrategyLegInput {
  strike: number;
  optionType: string;
  position: string;
  premium: number;
  iv: number;
  quantity?: number;
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
  error: string | null;
}

// ------------------------------------------------------------------
// Pricing Engine Toggle
// ------------------------------------------------------------------

export type PricingEngineType = 'legacy' | 'quantlib';
export type QuantLibEngine = 'analytic_bs' | 'binomial_crr' | 'binomial_jr' | 'binomial_lr' | 'finite_diff' | 'monte_carlo';

export interface QuantLibStatusResult {
  available: boolean;
  version: string | null;
  engines: string[];
}

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

export interface QuantLibLegResult {
  engine: string;
  price: number;
  delta: number;
  gamma: number;
  theta: number;
  vega: number;
  rho: number;
  d1: number | null;
  d2: number | null;
}

export interface QuantLibStrategyResult {
  success: boolean;
  engine: string;
  netPrice: number;
  netDelta: number;
  netGamma: number;
  netTheta: number;
  netVega: number;
  netRho: number;
  legs: QuantLibLegResult[];
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

// ------------------------------------------------------------------
// Indicator Table (TradingView-style)
// ------------------------------------------------------------------

export interface IndicatorTableResult {
  success: boolean;
  ticker: string;
  rowCount: number;
  columns: string[];
  rows: string[]; // JSON-serialized row dicts
  error?: string;
}

export interface IndicatorTableRow {
  time: number;
  open: number | null;
  high: number | null;
  low: number | null;
  close: number | null;
  volume: number | null;
  bb_basis: number | null;
  bb_upper: number | null;
  bb_lower: number | null;
  supertrend_up: number | null;
  supertrend_down: number | null;
  rsi: number | null;
  rsi_ma: number | null;
  macd: number | null;
  macd_signal: number | null;
  macd_histogram: number | null;
  adx: number | null;
  [key: string]: number | null; // dynamic EMA columns like ema_5, ema_10, etc.
}

// Rule-Based Backtest types
export interface RuleBasedBacktestResult {
  success: boolean;
  ticker: string;
  strategyName: string;
  parameters: string;
  totalTrades: number;
  winningTrades: number;
  losingTrades: number;
  winRate: number;
  avgWinPct: number;
  avgLossPct: number;
  winLossRatio: number;
  profitFactor: number;
  expectancyPerTrade: number;
  totalPnlPct: number;
  maxDrawdownPct: number;
  totalPnlPts: number;
  sharpeRatio: number;
  barsProcessed: number;
  trades: RuleBasedTrade[];
  error: string | null;
}

export interface RuleBasedTrade {
  tradeNumber: number;
  tradeType: string;
  entryTimestamp: string;
  exitTimestamp: string;
  entryPrice: number;
  exitPrice: number;
  pnl: number;
  pnlPct: number;
  cumulativePnlPct: number;
  signalReason: string;
  emaFast: number | null;
  emaSlow: number | null;
  emaGap: number | null;
  rsi: number | null;
  adx: number | null;
}

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

export interface SmartAggregatesResult {
  ticker: string;
  aggregates: StockAggregate[];
  summary: AggregatesSummary | null;
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
  message: string | null;
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

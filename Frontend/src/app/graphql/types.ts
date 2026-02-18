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

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

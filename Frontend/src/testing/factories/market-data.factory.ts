import { StockAggregate, AggregatesSummary, Ticker, IndicatorSeries } from '../../app/graphql/types';

export function createMockAggregate(overrides: Partial<StockAggregate> = {}): StockAggregate {
  return {
    id: 1,
    tickerId: 1,
    open: 150.0,
    high: 155.0,
    low: 148.0,
    close: 153.0,
    volume: 1000000,
    volumeWeightedAveragePrice: 152.0,
    timestamp: '2026-01-15T00:00:00.000Z',
    timespan: 'day',
    multiplier: 1,
    transactionCount: 50000,
    ...overrides,
  };
}

export function createMockAggregates(count: number): StockAggregate[] {
  return Array.from({ length: count }, (_, i) => {
    const date = new Date(2026, 0, i + 1);
    return createMockAggregate({
      id: i + 1,
      timestamp: date.toISOString(),
      open: 150 + i,
      high: 155 + i,
      low: 148 + i,
      close: 153 + i,
    });
  });
}

export function createMockSummary(overrides: Partial<AggregatesSummary> = {}): AggregatesSummary {
  return {
    periodHigh: 200,
    periodLow: 140,
    averageVolume: 500000,
    averageVwap: 170,
    openPrice: 150,
    closePrice: 190,
    priceChange: 40,
    priceChangePercent: 26.67,
    totalBars: 60,
    ...overrides,
  };
}

export function createMockTicker(overrides: Partial<Ticker> = {}): Ticker {
  return {
    id: 1,
    symbol: 'AAPL',
    name: 'Apple Inc.',
    market: 'stocks',
    locale: 'us',
    primaryExchange: 'XNAS',
    type: 'CS',
    active: true,
    currencySymbol: '$',
    createdAt: '2026-01-01T00:00:00Z',
    updatedAt: null,
    sanitizationSummary: null,
    ...overrides,
  };
}

export function createMockIndicatorSeries(overrides: Partial<IndicatorSeries> = {}): IndicatorSeries {
  return {
    name: 'sma',
    window: 20,
    data: Array.from({ length: 10 }, (_, i) => ({
      timestamp: new Date(2026, 0, i + 21).getTime(),
      value: 150 + i * 0.5,
      signal: null,
      histogram: null,
      upper: null,
      lower: null,
    })),
    ...overrides,
  };
}

import { StockAggregate } from '../../app/graphql/types';

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
    timestamp: Date.UTC(2026, 0, 15),
    timespan: 'day',
    multiplier: 1,
    transactionCount: 50000,
    ...overrides,
  };
}

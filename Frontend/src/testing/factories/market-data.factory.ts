import { StockAggregate } from '../../app/services/market-data.types';

export function createMockAggregate(overrides: Partial<StockAggregate> = {}): StockAggregate {
  return {
    open: 150.0,
    high: 155.0,
    low: 148.0,
    close: 153.0,
    volume: 1000000,
    vwap: 152.0,
    timestamp: Date.UTC(2026, 0, 15),
    transactions: 50000,
    ...overrides,
  };
}

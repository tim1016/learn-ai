import { describe, it, expect } from 'vitest';
import {
  multiTickerRangeToWire,
  tickerRangeToWire,
} from './ticker-wire';
import type { TickerRange } from '../shared/ticker-range-picker/ticker-range-picker.types';
import type { MultiTickerRange } from '../shared/multi-ticker-range-picker/multi-ticker-range-picker.types';

describe('tickerRangeToWire', () => {
  it('translates resolution=daily to timespan=day', () => {
    const r: TickerRange = {
      symbol: 'SPY',
      from: '2025-01-01',
      to: '2025-01-31',
      resolution: 'daily',
    };
    expect(tickerRangeToWire(r).timespan).toBe('day');
  });

  it('passes resolution=minute through as timespan=minute', () => {
    const r: TickerRange = {
      symbol: 'SPY',
      from: '2025-01-01',
      to: '2025-01-31',
      resolution: 'minute',
    };
    expect(tickerRangeToWire(r).timespan).toBe('minute');
  });

  it('passes resolution=hour through as timespan=hour', () => {
    const r: TickerRange = {
      symbol: 'SPY',
      from: '2025-01-01',
      to: '2025-01-31',
      resolution: 'hour',
    };
    expect(tickerRangeToWire(r).timespan).toBe('hour');
  });

  it('defaults multiplier to 1 when undefined', () => {
    const r: TickerRange = {
      symbol: 'SPY',
      from: '2025-01-01',
      to: '2025-01-31',
      resolution: 'minute',
    };
    expect(tickerRangeToWire(r).multiplier).toBe(1);
  });

  it('preserves explicit multiplier', () => {
    const r: TickerRange = {
      symbol: 'SPY',
      from: '2025-01-01',
      to: '2025-01-31',
      resolution: 'minute',
      multiplier: 5,
    };
    expect(tickerRangeToWire(r).multiplier).toBe(5);
  });

  it('defaults session to rth', () => {
    const r: TickerRange = {
      symbol: 'SPY',
      from: '2025-01-01',
      to: '2025-01-31',
      resolution: 'minute',
    };
    expect(tickerRangeToWire(r).session).toBe('rth');
  });

  it('preserves explicit session', () => {
    const r: TickerRange = {
      symbol: 'SPY',
      from: '2025-01-01',
      to: '2025-01-31',
      resolution: 'minute',
      session: 'extended',
    };
    expect(tickerRangeToWire(r).session).toBe('extended');
  });

  it('produces snake_case fields matching the Python TickerRequest schema', () => {
    const r: TickerRange = {
      symbol: 'SPY',
      from: '2025-01-01',
      to: '2025-01-31',
      resolution: 'minute',
    };
    const w = tickerRangeToWire(r);
    expect(Object.keys(w).sort()).toEqual([
      'from_date',
      'multiplier',
      'session',
      'symbol',
      'timespan',
      'to_date',
    ]);
  });
});

describe('multiTickerRangeToWire', () => {
  it('preserves the symbols array as a defensive copy', () => {
    const symbols = ['SPY', 'QQQ'];
    const r: MultiTickerRange = {
      symbols,
      from: '2025-01-01',
      to: '2025-01-31',
      resolution: 'minute',
    };
    const w = multiTickerRangeToWire(r);
    expect(w.symbols).toEqual(['SPY', 'QQQ']);
    expect(w.symbols).not.toBe(symbols); // defensive copy
  });

  it('produces the same sampling fields as the single-shape adapter', () => {
    const r: MultiTickerRange = {
      symbols: ['SPY'],
      from: '2025-01-01',
      to: '2025-01-31',
      resolution: 'daily',
      multiplier: 1,
    };
    const w = multiTickerRangeToWire(r);
    expect(w.timespan).toBe('day');
    expect(w.multiplier).toBe(1);
    expect(w.session).toBe('rth');
  });

  it('produces snake_case fields matching MultiTickerRequest', () => {
    const r: MultiTickerRange = {
      symbols: ['SPY', 'QQQ'],
      from: '2025-01-01',
      to: '2025-01-31',
      resolution: 'minute',
    };
    const w = multiTickerRangeToWire(r);
    expect(Object.keys(w).sort()).toEqual([
      'from_date',
      'multiplier',
      'session',
      'symbols',
      'timespan',
      'to_date',
    ]);
  });
});

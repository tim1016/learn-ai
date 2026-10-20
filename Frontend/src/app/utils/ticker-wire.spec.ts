import { describe, it, expect } from 'vitest';
import {
  multiTickerRangeToWire,
  tickerRangeToWire,
} from './ticker-wire';
import type { TickerRange } from '../shared/ticker-range-picker/ticker-range-picker.types';
import type { MultiTickerRange } from '../shared/multi-ticker-range-picker/multi-ticker-range-picker.types';

describe('tickerRangeToWire', () => {
  it.each<[TickerRange['resolution'], string]>([
    ['daily', 'day'],
    ['minute', 'minute'],
    ['hour', 'hour'],
  ])('maps resolution=%s to its wire timespan %s', (resolution, timespan) => {
    const r: TickerRange = {
      symbol: 'SPY',
      from: '2025-01-01',
      to: '2025-01-31',
      resolution,
    };
    expect(tickerRangeToWire(r).timespan).toBe(timespan);
  });

  it('defaults multiplier to 1 and session to rth, and produces snake_case fields matching the Python TickerRequest schema', () => {
    const r: TickerRange = {
      symbol: 'SPY',
      from: '2025-01-01',
      to: '2025-01-31',
      resolution: 'minute',
    };
    const w = tickerRangeToWire(r);
    expect.soft(w.multiplier, 'defaults multiplier to 1 when undefined').toBe(1);
    expect.soft(w.session, 'defaults session to rth').toBe('rth');
    expect.soft(Object.keys(w).sort(), 'produces snake_case fields matching the Python TickerRequest schema').toEqual([
      'from_date',
      'multiplier',
      'session',
      'symbol',
      'timespan',
      'to_date',
    ]);
  });

  it('preserves an explicit multiplier and an explicit session', () => {
    const withMultiplier: TickerRange = {
      symbol: 'SPY',
      from: '2025-01-01',
      to: '2025-01-31',
      resolution: 'minute',
      multiplier: 5,
    };
    const withSession: TickerRange = {
      symbol: 'SPY',
      from: '2025-01-01',
      to: '2025-01-31',
      resolution: 'minute',
      session: 'extended',
    };
    expect.soft(tickerRangeToWire(withMultiplier).multiplier, 'preserves explicit multiplier').toBe(5);
    expect.soft(tickerRangeToWire(withSession).session, 'preserves explicit session').toBe('extended');
  });
});

describe('multiTickerRangeToWire', () => {
  it('copies the symbols array and produces snake_case fields matching MultiTickerRequest', () => {
    const symbols = ['SPY', 'QQQ'];
    const r: MultiTickerRange = {
      symbols,
      from: '2025-01-01',
      to: '2025-01-31',
      resolution: 'minute',
    };
    const w = multiTickerRangeToWire(r);
    expect.soft(w.symbols, 'preserves the symbols array').toEqual(['SPY', 'QQQ']);
    expect.soft(w.symbols, 'preserves the symbols array as a defensive copy').not.toBe(symbols);
    expect.soft(Object.keys(w).sort(), 'produces snake_case fields matching MultiTickerRequest').toEqual([
      'from_date',
      'multiplier',
      'session',
      'symbols',
      'timespan',
      'to_date',
    ]);
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
});

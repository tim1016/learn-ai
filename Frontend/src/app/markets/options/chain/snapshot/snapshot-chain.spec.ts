import { describe, expect, it } from 'vitest';

import type { SnapshotContractResult, SnapshotUnderlyingResult, StockTickerSnapshot } from '../../../../services/market-data.types';
import { optionExpiryAnchorMs } from '../../../../shared/option-contract/option-expiry-date';
import type { ChainContractCell, ChainField, ChainPresentation, ChainRow } from '../chain-presentation';
import { snapshotPresentation, type SnapshotChainInputs } from './snapshot-chain';

const EXPIRY = optionExpiryAnchorMs('2026-10-16');

function contractAt(contractType: 'call' | 'put', strike: number, overrides: Partial<SnapshotContractResult> = {}): SnapshotContractResult {
  return {
    ticker: `O:SPY${strike}${contractType[0].toUpperCase()}`,
    contractType,
    strikePrice: strike,
    expirationMs: EXPIRY,
    breakEvenPrice: null,
    impliedVolatility: 0.2,
    openInterest: 100,
    greeks: { delta: 0.5, gamma: 0.01, theta: -0.05, vega: 0.1 },
    day: { open: 1, high: 1, low: 1, close: 1, volume: 1000, vwap: 1 },
    lastTrade: null,
    lastQuote: null,
    ...overrides,
  };
}

const both = (strike: number): SnapshotContractResult[] => [contractAt('call', strike), contractAt('put', strike)];
const underlying = (price: number): SnapshotUnderlyingResult => ({ ticker: 'SPY', price, change: 0, changePercent: 0 });

function present(overrides: Partial<SnapshotChainInputs> = {}): ChainPresentation {
  return snapshotPresentation({
    symbol: 'SPY',
    expiryMs: EXPIRY,
    contracts: [],
    underlying: underlying(720),
    stock: null,
    strikeRange: 15,
    showAll: false,
    density: 'quick',
    receivedAtMs: 1_791_559_000_000,
    refreshError: null,
    todayEt: '2026-10-14',
    ...overrides,
  });
}

const rowOf = (chain: ChainPresentation, strike: number): ChainRow => {
  const row = chain.table.rows.find((candidate) => candidate.strike === strike.toFixed(2));
  if (row === undefined) throw new Error(`No row at ${strike}.`);
  return row;
};
const cell = (side: ChainRow['call']): ChainContractCell => {
  if (side.kind !== 'contract') throw new Error('The side holds no contract.');
  return side;
};
const textOf = (field: ChainField): string => (field.kind === 'value' ? field.text : '—');
const fact = (chain: ChainPresentation, label: string) => chain.facts.find((each) => each.label === label)?.value;

describe('Now · Snapshot, as the one chain presentation', () => {
  it('marks the strike closest to spot as the money, and shades the sides that are in the money', () => {
    const chain = present({ contracts: [720, 723, 725, 730].flatMap(both), underlying: underlying(724) });

    expect(chain.table.rows.filter((row) => row.atm).map((row) => row.strike)).toEqual(['723.00']);
    expect(rowOf(chain, 723).mark).toBe('ATM 724.00');
    expect(rowOf(chain, 720).itm).toBe('call');
    expect(rowOf(chain, 725).itm).toBe('put');
    expect(rowOf(chain, 723).itm).toBeNull();
  });

  it('centres on the stock snapshot\'s day close, and on the chain underlying\'s price when there is none', () => {
    const stock = { day: { open: 1, high: 1, low: 1, close: 701.5, volume: 1, vwap: 1 } } as StockTickerSnapshot;
    const contracts = [701, 702, 700].flatMap(both);

    expect(present({ contracts, stock, underlying: underlying(700) }).table.rows.filter((row) => row.atm).map((row) => row.strike)).toEqual(['701.00']);
    expect(present({ contracts, stock: null, underlying: underlying(700) }).table.rows.filter((row) => row.atm).map((row) => row.strike)).toEqual(['700.00']);
    expect(present({ contracts, stock: null, underlying: null }).table.rows.filter((row) => row.atm)).toEqual([]);
  });

  it('limits the strikes to the range around the money, or shows every one', () => {
    const contracts = Array.from({ length: 41 }, (_, index) => 700 + index).flatMap(both);

    const narrow = present({ contracts, underlying: underlying(720), strikeRange: 5 });
    const all = present({ contracts, underlying: underlying(720), strikeRange: 5, showAll: true });

    expect(narrow.table.rows.map((row) => row.strike)).toEqual(['715.00', '716.00', '717.00', '718.00', '719.00', '720.00', '721.00', '722.00', '723.00', '724.00', '725.00']);
    expect(all.table.rows).toHaveLength(41);
  });

  it('draws what the provider sent as it always did, and a dash for what it did not', () => {
    const call = contractAt('call', 720, { lastQuote: null, day: { open: 0, high: 0, low: 0, close: 0, volume: 5000, vwap: 0 } });
    const put = contractAt('put', 720, { lastQuote: null, day: { open: 0, high: 0, low: 0, close: 0, volume: 0, vwap: 0 } });
    const row = rowOf(present({ contracts: [call, put] }), 720);

    expect(cell(row.call).fields.bid).toEqual({ kind: 'absent', reason: 'not_in_snapshot', detail: null });
    expect(textOf(cell(row.call).fields.volume)).toBe('5.0K');
    expect(cell(row.put).fields.volume.kind).toBe('absent');
    expect(textOf(cell(row.call).fields.delta)).toBe('0.500');
    expect(textOf(cell(row.call).fields.openInterest)).toBe('100');
    expect(row.centre && textOf(row.centre)).toBe('20.00%');
  });

  it.each([
    ['the day close', contractAt('call', 720, { day: { open: 1, high: 1, low: 1, close: 1.11, volume: 1, vwap: 1 }, lastTrade: { price: 2.22, size: 1, exchange: 1, timeframe: null } }), '1.11'],
    ['the last trade', contractAt('call', 720, { day: null, lastTrade: { price: 2.22, size: 1, exchange: 1, timeframe: null } }), '2.22'],
    ['the quote midpoint', contractAt('call', 720, { day: null, lastQuote: { bid: 3, ask: 4, bidSize: 1, askSize: 1, midpoint: 3.33, timeframe: null } }), '3.33'],
    ['the bid and ask middle', contractAt('call', 720, { day: null, lastQuote: { bid: 3, ask: 4, bidSize: 1, askSize: 1, midpoint: null, timeframe: null } }), '3.50'],
  ])('reads the last price from %s first', (_name, call, expected) => {
    expect(textOf(cell(rowOf(present({ contracts: [call] }), 720).call).fields.last)).toBe(expected);
  });

  it('gives every contract every field, each filled or absent, and says nothing of whether a contract is standard', () => {
    const row = rowOf(present({ contracts: both(720) }), 720);

    expect(Object.keys(cell(row.call).fields).sort()).toEqual(['ask', 'bid', 'delta', 'deltaGap', 'engineDelta', 'gamma', 'iv', 'last', 'openInterest', 'spread', 'theta', 'vega', 'volume']);
    expect(cell(row.call).standard).toBeNull();
  });

  it.each([
    ['quick', { calls: ['delta', 'last', 'volume'], puts: ['volume', 'last', 'delta'] }],
    ['flow', { calls: ['bid', 'ask', 'openInterest', 'volume'], puts: ['volume', 'openInterest', 'bid', 'ask'] }],
  ] as const)('draws the %s density\'s columns, calls outward to inward and puts inward to outward', (density, columns) => {
    expect(present({ density }).table.columns).toEqual({ ...columns, detail: 'compact' });
  });

  it('says which source, when the quotes arrived, and the underlying and expiry the chain is of', () => {
    const chain = present({ contracts: both(720), stock: { todaysChange: 2.5, todaysChangePercent: 0.35, day: { open: 1, high: 1, low: 1, close: 700, volume: 1, vwap: 1 } } as StockTickerSnapshot });

    expect(chain).toMatchObject({ mode: 'now-snapshot', underlying: 'SPY', expiryMs: EXPIRY });
    expect(fact(chain, 'Mode')).toEqual({ kind: 'text', text: 'Now · Snapshot' });
    expect(fact(chain, 'Source')).toEqual({ kind: 'text', text: 'Polygon option-chain snapshot, read through the data service.' });
    expect(fact(chain, 'Quotes received')).toEqual({ kind: 'instant', ms: 1_791_559_000_000, mode: 'local', granularity: 'time' });
    expect(fact(chain, 'Underlying')).toEqual({ kind: 'text', text: 'SPY 700.00, +2.50 (+0.35%)' });
    expect(fact(chain, 'Expiry')).toEqual({ kind: 'expiry', ms: EXPIRY, tag: null });
  });

  it('withholds the move, and says so, when the stock snapshot is not there', () => {
    expect(fact(present({ stock: null }), 'Underlying')).toEqual({ kind: 'text', text: 'SPY 720.00, move —' });
  });

  it.each([
    ['2026-10-14', '2 DTE'],
    ['2026-10-16', '0 DTE'],
    ['2026-10-20', '0 DTE'],
  ])('counts calendar days from %s to the expiry as %s, never below zero', (today, expected) => {
    expect(fact(present({ todayEt: today }), 'Days to expiry')).toEqual({ kind: 'text', text: expected });
  });

  it.each([
    ['delayed', [contractAt('call', 720, { lastQuote: { bid: 1, ask: 2, bidSize: 1, askSize: 1, midpoint: 1.5, timeframe: 'DELAYED' } })], /^Delayed: the provider labels/, 'warn'],
    ['real-time', [contractAt('call', 720, { lastQuote: { bid: 1, ask: 2, bidSize: 1, askSize: 1, midpoint: 1.5, timeframe: 'REAL-TIME' } })], /^Real-time: the provider labels/, undefined],
    ['unstated', [contractAt('call', 720)], /^Not stated: the provider does not say/, undefined],
  ])('says the quotes are %s as the provider labels them, and states nothing it was not told', (_name, contracts, words, tone) => {
    const state = present({ contracts }).facts.find((each) => each.label === 'Quote state');

    expect(state?.value).toMatchObject({ kind: 'text', text: expect.stringMatching(words) });
    expect(state?.tone).toBe(tone);
  });

  it('says the quotes are stale, with the reason, when the last refresh failed', () => {
    const state = present({ contracts: both(720), refreshError: 'Gateway timeout' }).facts.find((each) => each.label === 'Quote state');

    expect(state?.tone).toBe('bad');
    expect(state?.value).toEqual({ kind: 'text', text: 'Stale: the last refresh failed (Gateway timeout). These quotes are from the last refresh that worked.' });
  });
});

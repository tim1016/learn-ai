import { describe, expect, it } from 'vitest';

import { readInstrumentContext, sameContext, unreadQueryKeys, type InstrumentContext } from './instrument-context';

function read(query: Record<string, string>) {
  return readInstrumentContext({ get: (name) => query[name] ?? null });
}

const DEFAULT_SETTINGS = { adjustment: 'polygon_split_adjusted', forwardFill: true } as const;

describe('readInstrumentContext', () => {
  it('reads a complete window with its timeframe and session', () => {
    expect(read({ from: '2026-04-15', to: '2026-05-15', timeframe: '5m', session: 'extended' })).toEqual({
      kind: 'ready',
      context: { window: { from: '2026-04-15', to: '2026-05-15' }, timeframe: '5m', session: 'extended', ...DEFAULT_SETTINGS },
    });
  });

  it('defaults the timeframe, the session, the price adjustment and forward fill, but never the window', () => {
    expect(read({})).toEqual({ kind: 'window-missing', timeframe: '1d', session: 'regular', ...DEFAULT_SETTINGS });
    expect(read({ from: '2026-04-15', to: '2026-05-15' })).toMatchObject({
      kind: 'ready',
      context: { timeframe: '1d', session: 'regular', ...DEFAULT_SETTINGS },
    });
  });

  it('reads the raw price adjustment and the forward fill being off from the address, and keeps them with no window', () => {
    expect(read({ from: '2026-04-15', to: '2026-05-15', adjustment: 'raw', forwardFill: 'off' })).toMatchObject({
      kind: 'ready',
      context: { adjustment: 'raw', forwardFill: false },
    });
    expect(read({ adjustment: 'raw' })).toMatchObject({ kind: 'window-missing', adjustment: 'raw', forwardFill: true });
    expect(read({ from: '2026-04-15', to: '2026-05-15', adjustment: 'polygon_split_adjusted', forwardFill: 'on' })).toMatchObject({
      kind: 'ready',
      context: DEFAULT_SETTINGS,
    });
  });

  it.each([
    ['a price adjustment', { adjustment: 'dividend' }, /price adjustment "dividend" is not one this place offers/],
    ['a forward-fill choice', { forwardFill: 'maybe' }, /forward-fill choice "maybe" is not one this place offers/],
  ])('refuses %s this place does not offer, and reads the default beside it', (_name, query, problem) => {
    const result = read({ from: '2026-04-15', to: '2026-05-15', ...query });

    expect(result).toMatchObject({ kind: 'invalid', ...DEFAULT_SETTINGS });
    expect(result.kind === 'invalid' && result.problems.join(' ')).toMatch(problem);
  });

  it.each([
    ['half a window', { from: '2026-04-15' }, /needs both a start date and an end date/],
    ['the other half', { to: '2026-04-15' }, /needs both a start date and an end date/],
    ['a reversed window', { from: '2026-05-15', to: '2026-04-15' }, /start date is after the end date/],
    ['a day that does not exist', { from: '2026-02-30', to: '2026-03-15' }, /"2026-02-30" is not a date/],
    ['text typed into a date', { from: '2026-04-15', to: 'soon' }, /"soon" is not a date/],
    ['a month, half typed', { from: '2026-04', to: '2026-05-15' }, /"2026-04" is not a date/],
  ])('refuses %s, and says why', (_name, query, problem) => {
    const result = read(query);

    expect(result.kind).toBe('invalid');
    expect(result.kind === 'invalid' && result.problems.join(' ')).toMatch(problem);
  });

  it('refuses a timeframe or session this place does not offer, and keeps the one it does', () => {
    const result = read({ from: '2026-04-15', to: '2026-05-15', timeframe: '7m', session: 'regular' });

    expect(result).toMatchObject({ kind: 'invalid', timeframe: '1d', session: 'regular' });
    expect(read({ from: '2026-04-15', to: '2026-05-15', session: 'overnight' })).toMatchObject({ kind: 'invalid' });
  });

  it('does not take an unusable timeframe for a missing window', () => {
    expect(read({ timeframe: 'nope' }).kind).toBe('invalid');
  });
});

describe('sameContext', () => {
  const base: InstrumentContext = {
    window: { from: '2026-04-15', to: '2026-05-15' },
    timeframe: '1d',
    session: 'regular',
    ...DEFAULT_SETTINGS,
  };

  it('is the same request when every field is', () => {
    expect(sameContext(base, { ...base, window: { ...base.window } })).toBe(true);
  });

  it.each([
    ['the start', { window: { from: '2026-04-16', to: '2026-05-15' } }],
    ['the end', { window: { from: '2026-04-15', to: '2026-05-14' } }],
    ['the timeframe', { timeframe: '5m' }],
    ['the session', { session: 'extended' as const }],
    ['the price adjustment', { adjustment: 'raw' as const }],
    ['the forward fill', { forwardFill: false }],
  ])('is a different request when %s differs', (_name, change) => {
    expect(sameContext(base, { ...base, ...change })).toBe(false);
  });
});

describe('unreadQueryKeys', () => {
  it('names the keys the Chart, Returns and Data tabs do not read, once each, in address order', () => {
    expect(unreadQueryKeys(['theme', 'from', 'to', 'timeframe', 'session', 'adjustment', 'forwardFill', 'sessionId', 'indicators', 'theme'])).toEqual([
      'theme',
      'indicators',
    ]);
  });

  it('names nothing for an address that carries only keys they read', () => {
    expect(unreadQueryKeys(['from', 'to'])).toEqual([]);
    expect(unreadQueryKeys([])).toEqual([]);
  });
});

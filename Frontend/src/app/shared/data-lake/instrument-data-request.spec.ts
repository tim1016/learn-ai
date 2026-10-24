import { describe, expect, it } from 'vitest';

import { sameInstrumentDataRequest, type InstrumentDataRequest } from './instrument-data-request';

const REQUEST: InstrumentDataRequest = { symbol: 'SPY', from: '2024-03-04', to: '2024-03-15', tree: 'polygon_split_adjusted' };

describe('sameInstrumentDataRequest', () => {
  it('is true for equal values however the object was built, and for two absent requests', () => {
    expect.soft(sameInstrumentDataRequest({ ...REQUEST }, { ...REQUEST })).toBe(true);
    expect.soft(sameInstrumentDataRequest(null, null)).toBe(true);
  });

  it.each([
    ['symbol', { symbol: 'QQQ' }],
    ['start', { from: '2024-03-05' }],
    ['end', { to: '2024-03-14' }],
    ['tree', { tree: 'raw' as const }],
  ])('is false when the %s differs', (_name, change) => {
    expect(sameInstrumentDataRequest(REQUEST, { ...REQUEST, ...change })).toBe(false);
  });

  it('is false for a request against none', () => {
    expect(sameInstrumentDataRequest(REQUEST, null)).toBe(false);
  });
});

import { describe, it, expect } from 'vitest';
import { parseOcc, parseOccForDisplay, formatOcc, OccTickerParts } from './occ-ticker';

describe('parseOcc', () => {
  it('parses a SPY call', () => {
    const parts = parseOcc('O:SPY260220C00689000');
    expect(parts).toEqual({
      underlying: 'SPY',
      expirationDate: '2026-02-20',
      contractType: 'call',
      strike: 689,
    });
  });

  it('parses a put with non-integer strike', () => {
    const parts = parseOcc('O:AAPL260117P00227500');
    expect(parts).toEqual({
      underlying: 'AAPL',
      expirationDate: '2026-01-17',
      contractType: 'put',
      strike: 227.5,
    });
  });

  it('parses a sub-dollar strike', () => {
    const parts = parseOcc('O:GME260117C00000500');
    expect(parts?.strike).toBe(0.5);
  });

  it('returns null for malformed tickers', () => {
    expect(parseOcc('SPY260220C00689000')).toBeNull();         // missing O: prefix
    expect(parseOcc('O:SPY26022C00689000')).toBeNull();         // 5-digit date
    expect(parseOcc('O:SPY260220X00689000')).toBeNull();        // bad type char
    expect(parseOcc('O:spy260220C00689000')).toBeNull();        // lowercase
    expect(parseOcc('')).toBeNull();
  });
});

describe('parseOccForDisplay', () => {
  it('formats display fields for a SPY call', () => {
    expect(parseOccForDisplay('O:SPY260220C00689000')).toEqual({
      underlying: 'SPY',
      expDate: 'Feb 20, 2026',
      expDateShort: '02/20/26',
      type: 'Call',
      strike: '$689.00',
    });
  });

  it('formats display fields for a put with non-integer strike', () => {
    expect(parseOccForDisplay('O:AAPL260117P00227500')).toEqual({
      underlying: 'AAPL',
      expDate: 'Jan 17, 2026',
      expDateShort: '01/17/26',
      type: 'Put',
      strike: '$227.50',
    });
  });

  it('returns null for malformed tickers', () => {
    expect(parseOccForDisplay('not-a-ticker')).toBeNull();
  });
});

describe('formatOcc', () => {
  it('formats a SPY call', () => {
    const t = formatOcc({
      underlying: 'SPY', expirationDate: '2026-02-20', contractType: 'call', strike: 689,
    });
    expect(t).toBe('O:SPY260220C00689000');
  });

  it('formats a put with non-integer strike', () => {
    const t = formatOcc({
      underlying: 'AAPL', expirationDate: '2026-01-17', contractType: 'put', strike: 227.5,
    });
    expect(t).toBe('O:AAPL260117P00227500');
  });

  it('formats a sub-dollar strike', () => {
    const t = formatOcc({
      underlying: 'GME', expirationDate: '2026-01-17', contractType: 'call', strike: 0.5,
    });
    expect(t).toBe('O:GME260117C00000500');
  });

  it('throws on invalid inputs', () => {
    expect(() => formatOcc({
      underlying: '', expirationDate: '2026-02-20', contractType: 'call', strike: 100,
    })).toThrow();
    expect(() => formatOcc({
      underlying: 'spy', expirationDate: '2026-02-20', contractType: 'call', strike: 100,
    })).toThrow();
    expect(() => formatOcc({
      underlying: 'SPY', expirationDate: '2026/02/20', contractType: 'call', strike: 100,
    })).toThrow();
    expect(() => formatOcc({
      underlying: 'SPY', expirationDate: '2026-02-20', contractType: 'call', strike: -1,
    })).toThrow();
  });
});

describe('round-trip parity (R5 §8.2)', () => {
  // Exhaustive coverage across the field-shape permutations: integer
  // and fractional strikes, 2-digit and 3-digit underlyings, calls and
  // puts, dates spanning year/month/day boundaries.
  const cases: OccTickerParts[] = [
    { underlying: 'SPY', expirationDate: '2026-02-20', contractType: 'call', strike: 689 },
    { underlying: 'SPY', expirationDate: '2026-02-20', contractType: 'put', strike: 689 },
    { underlying: 'AAPL', expirationDate: '2026-01-17', contractType: 'call', strike: 227.5 },
    { underlying: 'GME', expirationDate: '2026-01-17', contractType: 'call', strike: 0.5 },
    { underlying: 'QQQ', expirationDate: '2026-12-31', contractType: 'put', strike: 500.25 },
    { underlying: 'A', expirationDate: '2099-12-31', contractType: 'call', strike: 1 },
    { underlying: 'BRKB', expirationDate: '2026-06-19', contractType: 'put', strike: 99999.999 },
  ];

  it.each(cases)('round-trips %j', (parts) => {
    const ticker = formatOcc(parts);
    const reparsed = parseOcc(ticker);
    expect(reparsed).toEqual(parts);
  });
});

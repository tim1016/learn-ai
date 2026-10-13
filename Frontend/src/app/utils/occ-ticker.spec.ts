import { describe, it, expect } from 'vitest';
import { parseOcc, parseOccForDisplay } from './occ-ticker';

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

  it('rejects calendar-invalid dates that match the regex shape', () => {
    expect(parseOcc('O:SPY261301C00100000')).toBeNull();        // month 13
    expect(parseOcc('O:SPY260000C00100000')).toBeNull();        // month 0
    expect(parseOcc('O:SPY260132C00100000')).toBeNull();        // day 32 in Jan
    expect(parseOcc('O:SPY260230C00100000')).toBeNull();        // Feb 30
    expect(parseOcc('O:SPY270229C00100000')).toBeNull();        // 2027 not leap
  });

  it('accepts boundary leap-day dates', () => {
    // 2028 is a leap year (divisible by 4, not 100)
    expect(parseOcc('O:SPY280229C00100000')).toEqual({
      underlying: 'SPY',
      expirationDate: '2028-02-29',
      contractType: 'call',
      strike: 100,
    });
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

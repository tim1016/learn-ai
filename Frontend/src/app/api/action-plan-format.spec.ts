import { describe, expect, it } from 'vitest';
import type { OptionEntryLeg } from './action-plan.types';
import { formatNyDate, optionSummary } from './action-plan-format';

function makeOption(overrides: Partial<OptionEntryLeg> = {}): OptionEntryLeg {
  return {
    leg_id: 'spy_long_call',
    instrument: { kind: 'option', underlying: 'SPY' },
    position: 'long',
    qty_ratio: 1,
    right: 'call',
    strike: { selector: 'atm' },
    expiry: { selector: 'min_dte', days: 14 },
    ...overrides,
  };
}

describe('optionSummary', () => {
  it('formats long ATM call with min_dte', () => {
    expect(optionSummary(makeOption())).toBe('Long call · ATM · min_dte 14d');
  });

  it('renders positive atm_offset with a + sign', () => {
    expect(
      optionSummary(makeOption({ strike: { selector: 'atm_offset', offset: 5 } })),
    ).toBe('Long call · ATM+5 · min_dte 14d');
  });

  it('renders negative atm_offset without an extra sign', () => {
    expect(
      optionSummary(makeOption({ strike: { selector: 'atm_offset', offset: -5 } })),
    ).toBe('Long call · ATM-5 · min_dte 14d');
  });

  it('renders short put with nearest_weekly expiry', () => {
    expect(
      optionSummary(
        makeOption({ position: 'short', right: 'put', expiry: { selector: 'nearest_weekly' } }),
      ),
    ).toBe('Short put · ATM · nearest weekly');
  });

  it('renders absolute expiry as the NY date', () => {
    // 2026-06-25 16:00 EDT = 2026-06-25 20:00 UTC.
    expect(
      optionSummary(
        makeOption({ expiry: { selector: 'absolute', expiration_ms: 1_782_417_600_000 } }),
      ),
    ).toBe('Long call · ATM · 2026-06-25');
  });

  it('renders absolute strike as ``$N`` — Slice 1F broker-derived pick', () => {
    expect(
      optionSummary(makeOption({ strike: { selector: 'absolute', strike: 650 } })),
    ).toBe('Long call · $650 · min_dte 14d');
  });
});

describe('formatNyDate', () => {
  it('converts a UTC moment that crossed midnight back to the NY date', () => {
    // NY-side 2026-06-25 23:30 EDT = 2026-06-26 03:30 UTC.
    // The UTC date is 2026-06-26 but the NY date is 2026-06-25 — pins
    // the timestamp-policy boundary at the rendering layer.
    const ms = new Date('2026-06-25T23:30:00-04:00').getTime();
    expect(formatNyDate(ms)).toBe('2026-06-25');
  });
});

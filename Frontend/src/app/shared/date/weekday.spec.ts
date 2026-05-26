import { describe, expect, it } from 'vitest';

import { toMostRecentTradingDayIso, toMostRecentWeekday } from './weekday';

describe('toMostRecentWeekday', () => {
  it('returns the same date when input is already a weekday', () => {
    const wed = new Date(2026, 4, 27); // Wed 2026-05-27
    expect(toMostRecentWeekday(wed).toDateString()).toBe(wed.toDateString());
  });

  it('walks Saturday back to Friday', () => {
    const sat = new Date(2026, 3, 25); // Sat 2026-04-25
    const out = toMostRecentWeekday(sat);
    expect(out.getDay()).toBe(5); // Friday
    expect(out.getDate()).toBe(24);
  });

  it('walks Sunday back to Friday (two-day walk)', () => {
    const sun = new Date(2025, 4, 25); // Sun 2025-05-25
    const out = toMostRecentWeekday(sun);
    expect(out.getDay()).toBe(5); // Friday
    expect(out.getDate()).toBe(23);
  });

  it('does not mutate the input', () => {
    const sat = new Date(2026, 3, 25);
    const before = sat.getTime();
    toMostRecentWeekday(sat);
    expect(sat.getTime()).toBe(before);
  });
});

describe('toMostRecentTradingDayIso', () => {
  // PR #346 P1 regression: ``pickTicker`` previously did
  // ``new Date(t.last)`` (UTC parse) → local-time ``toMostRecentWeekday``
  // → UTC ``isoDate``. On west-of-UTC browsers a Monday ``t.last``
  // became local Sunday, the walk stepped to local Friday, and
  // ``isoDate`` re-serialized that as Saturday. This helper is
  // UTC-internal end-to-end so the bug is structurally impossible
  // — output depends only on the input string, not on the browser
  // timezone.

  it('returns the same day when input is already a weekday', () => {
    expect(toMostRecentTradingDayIso('2026-05-25')).toBe('2026-05-25');
  });

  it('walks a Saturday input back to Friday', () => {
    expect(toMostRecentTradingDayIso('2026-04-25')).toBe('2026-04-24');
  });

  it('walks a Sunday input back to Friday', () => {
    expect(toMostRecentTradingDayIso('2025-05-25')).toBe('2025-05-23');
  });

  it('applies daysOffset before the weekday walk (pickTicker -30 case)', () => {
    // Mon 2026-05-25 minus 30 = Sat 2026-04-25 → Fri 2026-04-24.
    expect(toMostRecentTradingDayIso('2026-05-25', -30)).toBe('2026-04-24');
  });

  it('output is independent of the browser timezone (regression of PR #346 P1)', () => {
    // The bug only manifested in west-of-UTC; the fix means the
    // helper's behavior is fully determined by the ISO input and the
    // numeric offset, regardless of where the browser thinks "now" is.
    // This test pins the contract: same inputs → same outputs every time.
    expect(toMostRecentTradingDayIso('2026-05-25')).toBe('2026-05-25');
    expect(toMostRecentTradingDayIso('2026-05-26')).toBe('2026-05-26');
    expect(toMostRecentTradingDayIso('2026-05-27')).toBe('2026-05-27');
  });
});

import { describe, expect, it } from 'vitest';

import { toMostRecentTradingDayIso, toNextTradingDayIso } from './weekday';

describe('toMostRecentTradingDayIso', () => {
  // PR #346 P1 regression: ``pickTicker`` previously did
  // ``new Date(t.last)`` (UTC parse) → local-time ``toMostRecentWeekday``
  // → UTC ``isoDate``. On west-of-UTC browsers a Monday ``t.last``
  // became local Sunday, the walk stepped to local Friday, and
  // ``isoDate`` re-serialized that as Saturday. This helper is
  // UTC-internal end-to-end so the bug is structurally impossible
  // — output depends only on the input string, not on the browser
  // timezone.

  it.each([
    ['returns the same day when input is already a weekday', '2026-05-25', undefined, '2026-05-25'],
    ['walks a Saturday input back to Friday', '2026-04-25', undefined, '2026-04-24'],
    ['walks a Sunday input back to Friday', '2025-05-25', undefined, '2025-05-23'],
    // Mon 2026-05-25 minus 30 = Sat 2026-04-25 → Fri 2026-04-24.
    ['applies daysOffset before the weekday walk (pickTicker -30 case)', '2026-05-25', -30, '2026-04-24'],
  ])('%s', (_name, iso, daysOffset, expected) => {
    expect(toMostRecentTradingDayIso(iso, daysOffset)).toBe(expected);
  });
});

describe('toNextTradingDayIso', () => {
  it.each([
    ['returns the same date when it is already a weekday', '2026-09-21', '2026-09-21'],
    ['walks a Saturday forward to Monday', '2026-04-25', '2026-04-27'],
    ['walks a Sunday forward to Monday', '2025-05-25', '2025-05-26'],
  ])('%s', (_name, iso, expected) => {
    expect(toNextTradingDayIso(iso)).toBe(expected);
  });

  // The reason this exists: a floor walked *backward* off a weekend lands
  // outside the bound it represents — which for a provider history floor is
  // the 403 the backward walk was supposed to avoid.
  it('never steps a floor earlier than itself', () => {
    for (const iso of ['2021-09-23', '2021-09-25', '2021-09-26', '2021-09-27']) {
      expect(toNextTradingDayIso(iso) >= iso).toBe(true);
    }
  });
});

import { describe, expect, it } from 'vitest';

import { optionExpiryAnchorMs, optionExpiryDate } from './option-expiry-date';

describe('an option expiry date as its instant', () => {
  // Parity with PythonDataService/app/options/expiry.py::option_expiry_anchor_ms:
  // 16:00 New York time on the date,
  // which is 20:00 UTC in summer and 21:00 UTC in winter. The two days New
  // York's clocks change are here because midnight to 16:00 is 15 or 17
  // hours on them, not 16.
  it.each([
    ['a summer Friday', '2026-10-09', Date.UTC(2026, 9, 9, 20, 0, 0)],
    ['a winter Friday', '2026-12-18', Date.UTC(2026, 11, 18, 21, 0, 0)],
    ['the day the clocks go forward', '2026-03-08', Date.UTC(2026, 2, 8, 20, 0, 0)],
    ['the day the clocks go back', '2026-11-01', Date.UTC(2026, 10, 1, 21, 0, 0)],
  ])('is 16:00 New York time on %s', (_when, date, anchorMs) => {
    expect(optionExpiryAnchorMs(date)).toBe(anchorMs);
    expect(optionExpiryDate(anchorMs)).toBe(date);
  });
});

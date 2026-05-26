import { describe, expect, it } from 'vitest';

import { toMostRecentWeekday } from './weekday';

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

import { describe, expect, it } from 'vitest';

import { etDayEndMs, etIsoDate, etMidnightMs, isoDateAfter, shiftIsoDateByMonths } from './et-midnight';

describe('etMidnightMs', () => {
  it.each([
    ['a winter date at 05:00Z (EST)', '2024-01-02', Date.UTC(2024, 0, 2, 5)],
    ['a summer date at 04:00Z (EDT)', '2024-07-01', Date.UTC(2024, 6, 1, 4)],
    // 2024-03-10: clocks jump at 02:00 ET; midnight is still EST.
    ['the spring-forward date itself, whose midnight is still EST', '2024-03-10', Date.UTC(2024, 2, 10, 5)],
    // 2024-11-03: clocks fall back at 02:00 ET; midnight is still EDT.
    ['the fall-back date itself, whose midnight is still EDT', '2024-11-03', Date.UTC(2024, 10, 3, 4)],
  ])('anchors %s', (_name, date, expected) => {
    expect(etMidnightMs(date)).toBe(expected);
  });

  it('round-trips through the ET calendar date, and keeps a year below 100 as given instead of reading it as 19xx', () => {
    for (const day of ['2024-01-02', '2024-03-10', '2024-07-01', '2024-11-03', '2025-12-31']) {
      expect.soft(etIsoDate(etMidnightMs(day)), 'round-trips through the ET calendar date').toBe(day);
      expect.soft(etIsoDate(etMidnightMs(day) - 1), 'round-trips through the ET calendar date: a millisecond earlier is the day before').not.toBe(day);
    }

    // Date.UTC(22, …) is 1922; a date field reports 0022 while 2022 is half typed.
    const ms = etMidnightMs('0022-11-01');
    expect.soft(new Date(ms).getUTCFullYear(), 'keeps a year below 100 as given instead of reading it as 19xx').toBe(22);
    expect.soft(etIsoDate(ms), 'keeps a year below 100 as given: the date reads back as 0022-11-01').toBe('0022-11-01');
  });

  it('etDayEndMs is the next ET midnight (a half-open end)', () => {
    expect(etDayEndMs('2024-01-31')).toBe(etMidnightMs('2024-02-01'));
    expect(etDayEndMs('2024-11-02')).toBe(etMidnightMs('2024-11-03'));
  });

  it.each([
    ['a malformed date', '2024/01/02', /YYYY-MM-DD/],
    ['year 0000, which no calendar date has', '0000-11-01', /year 0000/],
  ])('rejects %s', (_name, date, message) => {
    expect(() => etMidnightMs(date)).toThrow(message);
  });
});


describe('calendar-month arithmetic', () => {
  it('shifts by whole months, keeps a year below 100 as given, and makes a default window of whole months', () => {
    expect.soft(shiftIsoDateByMonths('2026-01-01', -24), 'shifts by whole months and clamps the day to the target month: back two years').toBe('2024-01-01');
    expect.soft(shiftIsoDateByMonths('2025-03-31', -1), 'shifts by whole months and clamps the day to the target month: back into a shorter month').toBe('2025-02-28');
    expect.soft(shiftIsoDateByMonths('2024-02-29', 12), 'shifts by whole months and clamps the day to the target month: forward from a leap day').toBe('2025-02-28');
    expect.soft(isoDateAfter('2025-12-31'), 'shifts by whole months and clamps the day to the target month: the day after a year end').toBe('2026-01-01');

    expect.soft(isoDateAfter('0022-12-31'), 'keeps a year below 100 as given: the day after a year end').toBe('0023-01-01');
    expect.soft(shiftIsoDateByMonths('0022-11-01', -12), 'keeps a year below 100 as given: back a year').toBe('0021-11-01');
    expect.soft(shiftIsoDateByMonths('0099-12-15', 1), 'keeps a year below 100 as given: forward across year 100').toBe('0100-01-15');

    // 2026-01-01 minus 730 days would land on 2024-01-02; 24 calendar months lands on 2024-01-01.
    const yesterday = '2025-12-31';
    expect.soft(shiftIsoDateByMonths(isoDateAfter(yesterday), -24), 'makes a default window that is a whole number of months, leap years included').toBe('2024-01-01');
  });
});

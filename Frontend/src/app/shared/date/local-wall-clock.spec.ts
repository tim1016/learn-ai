import { describe, expect, it } from 'vitest';

import { isCalendarDate, isClockMinute, localWallClock, localWallClockMs } from './local-wall-clock';

describe('the viewer’s local wall clock', () => {
  it('reads the local date and minute an instant falls in, and round-trips every minute boundary it reads', () => {
    const ms = new Date(2026, 8, 29, 14, 59, 30).getTime();

    expect.soft(localWallClock(ms), 'reads the local date and minute an instant falls in').toEqual({ date: '2026-09-29', clock: '14:59' });

    for (const boundary of [new Date(2026, 0, 2, 9, 5).getTime(), new Date(2026, 6, 31, 23, 59).getTime()]) {
      expect.soft(localWallClockMs(localWallClock(boundary)), 'round-trips every minute boundary it reads').toBe(boundary);
    }
  });

  it('turns a typed date and time into the instant from their numeric parts, and refuses a blank, malformed or impossible wall clock rather than guessing', () => {
    expect.soft(localWallClockMs({ date: '2026-11-27', clock: '11:59' }), 'turns a typed date and time into the instant from their numeric parts: 2026-11-27').toBe(new Date(2026, 10, 27, 11, 59).getTime());
    expect.soft(localWallClockMs({ date: '2027-01-04', clock: '08:30' }), 'turns a typed date and time into the instant from their numeric parts: 2027-01-04').toBe(new Date(2027, 0, 4, 8, 30).getTime());

    expect.soft(localWallClockMs({ date: '', clock: '15:59' }), 'refuses a blank wall clock rather than guessing: no date').toBeNull();
    expect.soft(localWallClockMs({ date: '2026-09-29', clock: '' }), 'refuses a blank wall clock rather than guessing: no time').toBeNull();
    expect.soft(localWallClockMs({ date: '09/29/2026', clock: '15:59' }), 'refuses a malformed wall clock rather than guessing').toBeNull();
    expect.soft(localWallClockMs({ date: '2026-09-31', clock: '15:59' }), 'refuses an impossible wall clock rather than guessing: a date that does not exist').toBeNull();
    expect.soft(localWallClockMs({ date: '2026-09-29', clock: '24:10' }), 'refuses an impossible wall clock rather than guessing: a time that does not exist').toBeNull();
  });

  it('judges the date and the time each on its own, so each field can say which is wrong', () => {
    expect(isCalendarDate('2026-09-30')).toBe(true);
    expect(isCalendarDate('2028-02-29')).toBe(true);
    expect(isCalendarDate('2026-09-31')).toBe(false);
    expect(isCalendarDate('2027-02-29')).toBe(false);
    expect(isCalendarDate('')).toBe(false);
    expect(isClockMinute('00:00')).toBe(true);
    expect(isClockMinute('23:59')).toBe(true);
    expect(isClockMinute('24:00')).toBe(false);
    expect(isClockMinute('12:60')).toBe(false);
    expect(isClockMinute('9:30')).toBe(false);
  });
});

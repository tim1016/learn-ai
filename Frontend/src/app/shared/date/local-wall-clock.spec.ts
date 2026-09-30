import { describe, expect, it } from 'vitest';

import { localWallClock, localWallClockMs } from './local-wall-clock';

describe('the viewer’s local wall clock', () => {
  it('reads the local date and minute an instant falls in', () => {
    const ms = new Date(2026, 8, 29, 14, 59, 30).getTime();

    expect(localWallClock(ms)).toEqual({ date: '2026-09-29', time: '14:59' });
  });

  it('turns a typed date and time into the instant from their numeric parts', () => {
    expect(localWallClockMs({ date: '2026-11-27', time: '11:59' })).toBe(new Date(2026, 10, 27, 11, 59).getTime());
    expect(localWallClockMs({ date: '2027-01-04', time: '08:30' })).toBe(new Date(2027, 0, 4, 8, 30).getTime());
  });

  it('round-trips every minute boundary it reads', () => {
    for (const ms of [new Date(2026, 0, 2, 9, 5).getTime(), new Date(2026, 6, 31, 23, 59).getTime()]) {
      expect(localWallClockMs(localWallClock(ms))).toBe(ms);
    }
  });

  it('refuses a blank, malformed or impossible wall clock rather than guessing', () => {
    expect(localWallClockMs({ date: '', time: '15:59' })).toBeNull();
    expect(localWallClockMs({ date: '2026-09-29', time: '' })).toBeNull();
    expect(localWallClockMs({ date: '09/29/2026', time: '15:59' })).toBeNull();
    expect(localWallClockMs({ date: '2026-09-31', time: '15:59' })).toBeNull();
    expect(localWallClockMs({ date: '2026-09-29', time: '24:10' })).toBeNull();
  });
});

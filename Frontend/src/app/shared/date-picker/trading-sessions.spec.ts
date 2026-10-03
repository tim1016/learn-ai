import { describe, expect, it } from 'vitest';

import { etMidnightMs } from '../date/et-midnight';
import { fakeSessions } from './testing/fake-trading-calendar';
import { TradingSessions } from './trading-sessions';

const HOUR_MS = 60 * 60 * 1000;

describe('TradingSessions', () => {
  it('reads the wire window as ET calendar dates', () => {
    // Thanksgiving 2024: Thursday closed, Friday a half-day closing at 18:00Z.
    const sessions = new TradingSessions(
      {
        session_open_ms: [Date.UTC(2024, 10, 27, 14, 30), Date.UTC(2024, 10, 29, 14, 30)],
        session_close_ms: [Date.UTC(2024, 10, 27, 21), Date.UTC(2024, 10, 29, 18)],
        holidays: [{ date_ms: Date.UTC(2024, 10, 28, 14, 30), name: 'Thanksgiving Day' }],
      },
      '2024-11-25',
      '2024-11-30',
    );

    expect(sessions.isSession('2024-11-27')).toBe(true);
    expect(sessions.isSession('2024-11-28')).toBe(false);
    expect(sessions.holidayName('2024-11-28')).toBe('Thanksgiving Day');
    expect(sessions.holidaysBetween('2024-11-25', '2024-11-30')).toEqual(['2024-11-28']);
    // The half-day has closed by 18:00Z; a full day would still be open.
    expect(sessions.lastCompleted(Date.UTC(2024, 10, 29, 18))).toBe('2024-11-29');
    expect(sessions.lastCompleted(Date.UTC(2024, 10, 29, 17, 59))).toBe('2024-11-27');
  });

  it('steps over weekends and holidays in both directions', () => {
    const sessions = fakeSessions('2024-08-26', '2024-09-13', { '2024-09-02': 'Labor Day' });

    // Saturday 2024-08-31, Sunday, Labor Day Monday.
    expect(sessions.onOrAfter('2024-08-31')).toBe('2024-09-03');
    expect(sessions.onOrBefore('2024-09-02')).toBe('2024-08-30');
    expect(sessions.onOrAfter('2024-09-03')).toBe('2024-09-03');
    expect(sessions.onOrBefore('2024-09-03')).toBe('2024-09-03');
  });

  it('counts the sessions in a range, both ends included', () => {
    const sessions = fakeSessions('2024-08-26', '2024-09-13', { '2024-09-02': 'Labor Day' });

    expect(sessions.count('2024-08-30', '2024-09-04')).toBe(3);
    expect(sessions.count('2024-08-31', '2024-09-02')).toBe(0);
    expect(sessions.count('2024-09-04', '2024-09-03')).toBe(0);
  });

  it('answers only inside the window it was read for', () => {
    const sessions = fakeSessions('2024-08-26', '2024-09-13');

    expect(sessions.covers('2024-08-26')).toBe(true);
    expect(sessions.covers('2024-08-25')).toBe(false);
    expect(sessions.onOrAfter('2024-09-14')).toBeNull();
    expect(sessions.lastCompleted(etMidnightMs('2024-08-26') + 9 * HOUR_MS)).toBeNull();
  });
});

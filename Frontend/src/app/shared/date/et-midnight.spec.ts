import { describe, expect, it } from 'vitest';

import { etDayEndMs, etIsoDate, etMidnightMs } from './et-midnight';

describe('etMidnightMs', () => {
  it('anchors a winter date at 05:00Z (EST)', () => {
    expect(etMidnightMs('2024-01-02')).toBe(Date.UTC(2024, 0, 2, 5));
  });

  it('anchors a summer date at 04:00Z (EDT)', () => {
    expect(etMidnightMs('2024-07-01')).toBe(Date.UTC(2024, 6, 1, 4));
  });

  it('handles the spring-forward and fall-back dates themselves', () => {
    // 2024-03-10: clocks jump at 02:00 ET; midnight is still EST.
    expect(etMidnightMs('2024-03-10')).toBe(Date.UTC(2024, 2, 10, 5));
    // 2024-11-03: clocks fall back at 02:00 ET; midnight is still EDT.
    expect(etMidnightMs('2024-11-03')).toBe(Date.UTC(2024, 10, 3, 4));
  });

  it('round-trips through the ET calendar date', () => {
    for (const day of ['2024-01-02', '2024-03-10', '2024-07-01', '2024-11-03', '2025-12-31']) {
      expect(etIsoDate(etMidnightMs(day))).toBe(day);
      expect(etIsoDate(etMidnightMs(day) - 1)).not.toBe(day);
    }
  });

  it('etDayEndMs is the next ET midnight (a half-open end)', () => {
    expect(etDayEndMs('2024-01-31')).toBe(etMidnightMs('2024-02-01'));
    expect(etDayEndMs('2024-11-02')).toBe(etMidnightMs('2024-11-03'));
  });

  it('rejects a malformed date', () => {
    expect(() => etMidnightMs('2024/01/02')).toThrow(/YYYY-MM-DD/);
  });
});

import { describe, expect, it } from 'vitest';

import { formatLocalClock, formatLocalTimestamp } from './local-timestamp';

// 2026-06-09 14:12:58.021 UTC → 10:12:58 in America/New_York (EDT, -04:00).
const TS_MS = 1781014378021;

describe('formatLocalTimestamp', () => {
  it('formats UTC as a literal "YYYY-MM-DD HH:MM:SS" string', () => {
    expect(formatLocalTimestamp(TS_MS, 'UTC')).toBe('2026-06-09 14:12:58');
  });

  it('honours the IANA zone arg (America/New_York, EDT)', () => {
    expect(formatLocalTimestamp(TS_MS, 'America/New_York')).toBe('2026-06-09 10:12:58');
  });

  it('honours the IANA zone arg (Asia/Tokyo, +09:00)', () => {
    expect(formatLocalTimestamp(TS_MS, 'Asia/Tokyo')).toBe('2026-06-09 23:12:58');
  });

  it('uses 00, not 24, at midnight (hourCycle h23)', () => {
    const midnightUtc = Date.UTC(2026, 0, 1, 0, 0, 0);
    expect(formatLocalTimestamp(midnightUtc, 'UTC')).toBe('2026-01-01 00:00:00');
  });
});

describe('formatLocalClock', () => {
  it('formats UTC as "HH:MM:SS"', () => {
    expect(formatLocalClock(TS_MS, 'UTC')).toBe('14:12:58');
  });

  it('honours the IANA zone arg', () => {
    expect(formatLocalClock(TS_MS, 'America/New_York')).toBe('10:12:58');
  });
});

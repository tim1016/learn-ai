import { describe, expect, it } from 'vitest';

import { etIsoDate, etMidnightMs, isoDateAfter } from '../../shared/date/et-midnight';
import { UNBOUNDED, type DateBounds } from '../../shared/date-picker';
import { providerBounds } from '../../shared/date-picker/provider-bounds.service';
import { NO_DATES, OFFERED_RANGES, priceBounds, rangeInstants, rangeOfStretch } from './duration';
import type { TestableStretch } from './options-backtest.types';
import { sessionOpenMs } from '../../shared/date-picker/testing/fake-trading-calendar';

/**
 * The instants below were worked out apart from the code, from the clock the
 * New York zone keeps: UTC-5 in winter (EST) and UTC-4 in summer (EDT), the
 * change on the second Sunday of March and the first Sunday of November.
 * Midnight is 05:00 UTC in winter and 04:00 UTC in summer, and an epoch day
 * is 86,400,000 ms (2026-03-06 is day 20,518; 2025-10-31 is day 20,392).
 */
describe('the two instants of a range', () => {
  it('is New York midnight of the first date to New York midnight after the last, for a winter week', () => {
    // 2026-01-12 is epoch day 20,465: 05:00 UTC. The midnight after Friday the 16th is Saturday the 17th, day 20,470.
    expect(rangeInstants({ from: '2026-01-12', to: '2026-01-16' })).toEqual({
      start_ms: 20_465 * 86_400_000 + 5 * 3_600_000,
      end_ms: 20_470 * 86_400_000 + 5 * 3_600_000,
    });
  });

  it('is New York midnight of the first date to New York midnight after the last, for a summer week', () => {
    // 2026-07-06 is epoch day 20,640 and the midnight after Friday the 10th is day 20,645: 04:00 UTC.
    expect(rangeInstants({ from: '2026-07-06', to: '2026-07-10' })).toEqual({
      start_ms: 20_640 * 86_400_000 + 4 * 3_600_000,
      end_ms: 20_645 * 86_400_000 + 4 * 3_600_000,
    });
  });

  it('keeps each end on its own side of the clock change in March (the 8th of March 2026)', () => {
    // Start: Friday 2026-03-06 00:00 EST = 05:00 UTC on day 20,518. End: Tuesday 2026-03-10 00:00 EDT = 04:00 UTC on day 20,522.
    expect(rangeInstants({ from: '2026-03-06', to: '2026-03-09' })).toEqual({ start_ms: 1_772_773_200_000, end_ms: 1_773_115_200_000 });
    expect(1_773_115_200_000 - 1_772_773_200_000).toBe(4 * 86_400_000 - 3_600_000);
  });

  it('keeps each end on its own side of the clock change in November (the 2nd of November 2025)', () => {
    // Start: Friday 2025-10-31 00:00 EDT = 04:00 UTC on day 20,392. End: Tuesday 2025-11-04 00:00 EST = 05:00 UTC on day 20,396.
    expect(rangeInstants({ from: '2025-10-31', to: '2025-11-03' })).toEqual({ start_ms: 1_761_883_200_000, end_ms: 1_762_232_400_000 });
    expect(1_762_232_400_000 - 1_761_883_200_000).toBe(4 * 86_400_000 + 3_600_000);
  });

  it('ends on the day the clocks go forward (Sunday the 8th of March 2026) at the midnight after it, which is 23 hours on, not 24', () => {
    // Start: Friday 2026-03-06 00:00 EST = 05:00 UTC on day 20,518. End: Monday 2026-03-09 00:00 EDT = 04:00 UTC on day 20,521.
    // Midnight of the 8th is 05:00 UTC on day 20,520, and a day later in the clock's own terms is 04:00 UTC on day 20,521, an hour earlier than 24 hours on.
    expect(rangeInstants({ from: '2026-03-06', to: '2026-03-08' })).toEqual({
      start_ms: 20_518 * 86_400_000 + 5 * 3_600_000,
      end_ms: 20_521 * 86_400_000 + 4 * 3_600_000,
    });
  });

  it('ends on the day the clocks go back (Sunday the 1st of November 2026) at the midnight after it, which is 25 hours on, not 24', () => {
    // Start: Friday 2026-10-30 00:00 EDT = 04:00 UTC on day 20,756. End: Monday 2026-11-02 00:00 EST = 05:00 UTC on day 20,759.
    // Midnight of the 1st is 04:00 UTC on day 20,758, and 24 hours on would be 04:00 UTC on day 20,759, an hour before the midnight.
    expect(rangeInstants({ from: '2026-10-30', to: '2026-11-01' })).toEqual({
      start_ms: 20_756 * 86_400_000 + 4 * 3_600_000,
      end_ms: 20_759 * 86_400_000 + 5 * 3_600_000,
    });
  });

  // The server refuses a range whose two instants are not New York midnights (OPTIONS_BACKTEST_RANGE_NOT_ON_MIDNIGHT). The
  // page always sends midnights, and the Sundays the clocks change are where a day that is not 24 hours long would show it.
  const isNewYorkMidnight = (ms: number) => etMidnightMs(etIsoDate(ms)) === ms;

  it.each([
    ['ending on the Sunday the clocks go forward', { from: '2026-03-06', to: '2026-03-08' }, '2026-03-09'],
    ['ending on the Sunday the clocks go back', { from: '2026-10-30', to: '2026-11-01' }, '2026-11-02'],
    ['starting on the Sunday the clocks go forward', { from: '2026-03-08', to: '2026-03-10' }, '2026-03-11'],
    ['starting on the Sunday the clocks go back', { from: '2026-11-01', to: '2026-11-03' }, '2026-11-04'],
    ['of the Sunday the clocks go forward alone', { from: '2026-03-08', to: '2026-03-08' }, '2026-03-09'],
    ['of the Sunday the clocks go back alone', { from: '2026-11-01', to: '2026-11-01' }, '2026-11-02'],
    ['of a plain winter week', { from: '2026-01-12', to: '2026-01-16' }, '2026-01-17'],
  ])('sends two New York midnights for a range %s: the first date’s and the day after the last date’s', (_name, range, dayAfter) => {
    const instants = rangeInstants(range);

    expect(instants).not.toBeNull();
    expect(isNewYorkMidnight(instants?.start_ms ?? Number.NaN)).toBe(true);
    expect(isNewYorkMidnight(instants?.end_ms ?? Number.NaN)).toBe(true);
    expect(etIsoDate(instants?.start_ms ?? Number.NaN)).toBe(range.from);
    expect(etIsoDate(instants?.end_ms ?? Number.NaN)).toBe(dayAfter);
    expect(isoDateAfter(range.to)).toBe(dayAfter);
  });

  it('is not a midnight at the end when the last date is made one day long by adding 24 hours: that instant is one in the morning (the test sees it)', () => {
    // The end the page must not send for a range ending on Sunday 2026-03-08: its midnight is 05:00 UTC, and +24 hours is 01:00 EDT on the 9th.
    const wrong = etMidnightMs('2026-03-08') + 86_400_000;

    expect(isNewYorkMidnight(wrong)).toBe(false);
    expect(rangeInstants({ from: '2026-03-06', to: '2026-03-08' })?.end_ms).not.toBe(wrong);
  });

  it('is one day wide for one date: the start is the date, the end the midnight after it', () => {
    const one = rangeInstants({ from: '2026-09-14', to: '2026-09-14' });

    expect(one).toEqual({ start_ms: 1_789_358_400_000, end_ms: 1_789_358_400_000 + 86_400_000 });
  });

  it('is nothing while either end holds no date, and never a made-up one', () => {
    expect(rangeInstants(NO_DATES)).toBeNull();
    expect(rangeInstants({ from: '2026-09-14', to: '' })).toBeNull();
    expect(rangeInstants({ from: '', to: '2026-09-14' })).toBeNull();
  });
});

describe('the picker’s bounds', () => {
  // The provider's bound for a run over completed sessions: its oldest day to the last completed session.
  const PROVIDER = providerBounds('2021-09-21', '2026-09-18');

  it('are the provider’s until the server has said from when option prices are available: its oldest day, and the last completed session', () => {
    expect(priceBounds(null, PROVIDER)).toEqual(PROVIDER);
    expect(priceBounds(null, PROVIDER)).toMatchObject({ max: '2026-09-18', maxReason: 'The last completed session is 2026-09-18.' });
  });

  it('start at the New York date of that instant, with the reason in words, and still end on the last completed session', () => {
    const bounds = priceBounds(etMidnightMs('2025-10-15'), PROVIDER);

    expect(bounds.min).toBe('2025-10-15');
    expect(bounds.minReason).toBe('Option prices are available from 2025-10-15, so no earlier day can be picked.');
    expect(bounds).toMatchObject({ max: '2026-09-18', maxReason: 'The last completed session is 2026-09-18.', unknown: null });
  });

  it('read an instant late on the evening before in New York as that evening’s date, not the next day’s', () => {
    // 2025-10-15 00:30 UTC is 2025-10-14 20:30 EDT.
    const bounds = priceBounds(etMidnightMs('2025-10-15') - 3_600_000 * 3.5, PROVIDER);

    expect(bounds.min).toBe('2025-10-14');
  });

  it('keep the provider’s oldest day when it is the later of the two, with its own reason', () => {
    const later = providerBounds('2026-01-05', '2026-09-18');

    expect(priceBounds(etMidnightMs('2025-10-15'), later)).toEqual(later);
  });

  it('keep checking the day option prices start from, and end nowhere, while the provider’s history has not been read', () => {
    const unread: DateBounds = { ...UNBOUNDED, unknown: 'The provider’s history is being read.' };

    expect(priceBounds(null, unread)).toEqual(unread);
    expect(priceBounds(etMidnightMs('2025-10-15'), unread)).toMatchObject({ min: '2025-10-15', max: null, unknown: null });
  });
});

describe('Run available dates', () => {
  it('writes the New York dates of the first and last session of the stretch', () => {
    const stretch: TestableStretch = {
      start_ms: etMidnightMs('2026-09-14'),
      end_ms: etMidnightMs('2026-09-19'),
      first_session_open_ms: sessionOpenMs('2026-09-14'),
      last_session_open_ms: sessionOpenMs('2026-09-18'),
      sessions: 5,
    };

    expect(rangeOfStretch(stretch)).toEqual({ from: '2026-09-14', to: '2026-09-18' });
  });
});

describe('the quick ranges', () => {
  it('leave out what reaches further back than the options plan does', () => {
    expect(OFFERED_RANGES).not.toContain('5Y');
    expect(OFFERED_RANGES).not.toContain('Max');
  });
});

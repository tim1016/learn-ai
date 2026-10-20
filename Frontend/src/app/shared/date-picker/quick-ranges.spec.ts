import { describe, expect, it } from 'vitest';

import { etMidnightMs } from '../date/et-midnight';
import { UNBOUNDED, heldBounds, type DateBounds } from './date-bounds';
import { QUICK_RANGES, activeQuickRanges, offerQuickRanges, type QuickRangeKey } from './quick-ranges';
import { fakeSessions } from './testing/fake-trading-calendar';

const HOLIDAYS = { '2024-09-02': 'Labor Day', '2026-01-01': "New Year's Day" };
const SESSIONS = fakeSessions('2021-01-01', '2026-12-31', HOLIDAYS);
/** At noon ET on Friday 2026-10-02 that session has not closed, so the last completed one is Thursday. */
const LAST_COMPLETED = SESSIONS.lastCompleted(etMidnightMs('2026-10-02') + 12 * 60 * 60 * 1000);
const QQQ = heldBounds('QQQ', '2024-09-03', '2026-09-18');

function offer(key: QuickRangeKey, bounds = QQQ) {
  return offerQuickRanges([key], bounds, SESSIONS, LAST_COMPLETED)[0];
}

describe('offerQuickRanges', () => {
  const fetched = { ...UNBOUNDED, min: '2021-10-05', minReason: 'The provider serves bars from 2021-10-05.' };

  it.each<{ key: QuickRangeKey; from: string; to: string; bounds?: DateBounds }>([
    // Every range ends on the last held day and is exactly its label, both ends included.
    { key: '1D', from: '2026-09-18', to: '2026-09-18' },
    // Seven days ending Friday the 18th start on Saturday the 12th, so the first session is Monday.
    { key: '1W', from: '2026-09-14', to: '2026-09-18' },
    { key: '1M', from: '2026-08-19', to: '2026-09-18' },
    // Three calendar months, not ninety days (which would start on 2026-06-20).
    { key: '3M', from: '2026-06-19', to: '2026-09-18' },
    { key: '1Y', from: '2025-09-19', to: '2026-09-18' },
    { key: '2Y', from: '2024-09-19', to: '2026-09-18' },
    // A start that falls on a closed day moves forward to the next session, never back:
    // 2026-01-01 is a holiday, so YTD starts on the 2nd; 2026-03-19 is a session and stays.
    { key: 'YTD', from: '2026-01-02', to: '2026-09-18' },
    { key: '6M', from: '2026-03-19', to: '2026-09-18' },
    // Max is the whole bound.
    { key: 'Max', from: '2024-09-03', to: '2026-09-18' },
    // A range whose start snaps onto the first held day: two years ending Tuesday 2026-09-01 start
    // on Monday 2024-09-02, Labor Day; Tuesday is the first held day.
    { key: '2Y', from: '2024-09-03', to: '2026-09-01', bounds: heldBounds('QQQ', '2024-09-03', '2026-09-01') },
    // With no last day of its own, a page's ranges end on the last completed session.
    { key: '1D', from: '2026-10-01', to: '2026-10-01', bounds: fetched },
    { key: 'Max', from: '2021-10-05', to: '2026-10-01', bounds: fetched },
  ])('offers $key as $from to $to', ({ key, from, to, bounds }) => {
    expect(offer(key, bounds).range).toEqual({ from, to });
  });

  it('leaves the start on its calendar day for a host whose dates need not be sessions, so the range is whole months', () => {
    // One month ending Monday 2026-09-14 starts on Saturday 2026-08-15.
    const bounds = heldBounds('QQQ', '2024-09-03', '2026-09-14');

    expect(offerQuickRanges(['1M'], bounds, SESSIONS, LAST_COMPLETED, { sessionsOnly: false })[0].range).toEqual({ from: '2026-08-15', to: '2026-09-14' });
    expect(offerQuickRanges(['1M'], bounds, SESSIONS, LAST_COMPLETED, { sessionsOnly: true })[0].range).toEqual({ from: '2026-08-17', to: '2026-09-14' });
  });

  it.each<[QuickRangeKey, DateBounds, string]>([
    ['5Y', QQQ, 'QQQ is held from 2024-09-03.'],
    // Two years ending 2026-09-18 start on 2024-09-19: a day short is refused, with no leeway.
    ['2Y', heldBounds('QQQ', '2024-09-20', '2026-09-18'), 'QQQ is held from 2024-09-20.'],
  ])('refuses %s when it starts before the first held day, with the reason', (key, bounds, reason) => {
    expect(offer(key, bounds)).toEqual({ key, range: null, unavailable: reason });
  });

  describe('5Y against a first day a few days inside five years', () => {
    const servedFrom = (min: string) => ({ ...UNBOUNDED, min, minReason: `The provider serves bars from ${min}.` });

    it('serves from the first day with a note, then refuses past a week short or for a host whose ranges must be exact', () => {
      // Five years ending 2026-10-01 start on Saturday 2021-10-02; the next session, the 4th, is before the floor.
      expect.soft(offer('5Y', servedFrom('2021-10-05')), 'starts on the first day and says so, where the exact start is a day short').toEqual({
        key: '5Y',
        range: { from: '2021-10-05', to: '2026-10-01' },
        unavailable: null,
        note: 'Starts on 2021-10-05. The provider serves bars from 2021-10-05.',
      });

      // The floor is a Saturday; the range must still start on a session.
      expect.soft(offer('5Y', servedFrom('2021-10-09')).range, 'starts on the first session on or after a first day that is closed').toEqual({ from: '2021-10-11', to: '2026-10-01' });

      const anyDay = (min: string) => offerQuickRanges(['5Y'], servedFrom(min), SESSIONS, LAST_COMPLETED, { sessionsOnly: false })[0];

      // The exact start is 2021-10-02: seven days short is served, eight is not.
      expect.soft(anyDay('2021-10-09').range, 'is refused once it falls more than a week short: seven days short is served').toEqual({ from: '2021-10-09', to: '2026-10-01' });
      expect.soft(anyDay('2021-10-10'), 'is refused once it falls more than a week short: eight days short is refused').toEqual({ key: '5Y', range: null, unavailable: 'The provider serves bars from 2021-10-10.' });

      // The start, Saturday 2021-10-02, moves to Monday the 4th, six days before the 10th; the range is still eight days short.
      expect.soft(offer('5Y', servedFrom('2021-10-10')), 'counts the week from the calendar start, not from the session the start moves to').toEqual({ key: '5Y', range: null, unavailable: 'The provider serves bars from 2021-10-10.' });

      const exact = offerQuickRanges(['5Y'], servedFrom('2021-10-05'), SESSIONS, LAST_COMPLETED, { exact: true })[0];

      expect.soft(exact, 'is refused however little it falls short, for a host whose ranges must be exact').toEqual({ key: '5Y', range: null, unavailable: 'The provider serves bars from 2021-10-05.' });

      expect.soft(offer('5Y', servedFrom('2021-10-01')), 'carries no note when the page reads all five years').toEqual({ key: '5Y', range: { from: '2021-10-04', to: '2026-10-01' }, unavailable: null });
    });
  });

  it('offers nothing while the span or the calendar is unknown, and says why', () => {
    const unknown = offerQuickRanges(QUICK_RANGES, heldBounds('', null, null), SESSIONS, LAST_COMPLETED);
    expect(unknown.every((o) => o.range === null && o.unavailable === 'Pick an instrument to see the dates it is held for.')).toBe(true);

    const unread = offerQuickRanges(['1M'], QQQ, null, LAST_COMPLETED);
    expect(unread).toEqual([{ key: '1M', range: null, unavailable: 'The trading calendar has not been read.' }]);
  });
});

describe('activeQuickRanges', () => {
  it('names a range only when the dates are exactly what its click would set', () => {
    const offers = offerQuickRanges(QUICK_RANGES, QQQ, SESSIONS, LAST_COMPLETED);

    expect([...activeQuickRanges(offers, '2026-08-19', '2026-09-18')]).toEqual(['1M']);
    expect([...activeQuickRanges(offers, '2026-08-20', '2026-09-18')]).toEqual([]);
    expect([...activeQuickRanges(offers, '', '')]).toEqual([]);
  });

  it('names every range whose click would set those dates', () => {
    // On a page bounded by the provider, 5Y and Max are the same dates.
    const fetched = { ...UNBOUNDED, min: '2021-10-05', minReason: 'The provider serves bars from 2021-10-05.' };
    const offers = offerQuickRanges(QUICK_RANGES, fetched, SESSIONS, LAST_COMPLETED);

    expect([...activeQuickRanges(offers, '2021-10-05', '2026-10-01')]).toEqual(['5Y', 'Max']);
  });
});

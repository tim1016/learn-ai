import { describe, expect, it } from 'vitest';

import { etMidnightMs } from '../date/et-midnight';
import { UNBOUNDED, heldBounds } from './date-bounds';
import { QUICK_RANGES, activeQuickRange, offerQuickRanges, type QuickRangeKey } from './quick-ranges';
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
  it('ends every range on the last held day and counts its start back on the calendar', () => {
    expect(offer('1D').range).toEqual({ from: '2026-09-18', to: '2026-09-18' });
    expect(offer('1W').range).toEqual({ from: '2026-09-11', to: '2026-09-18' });
    // One calendar month, not thirty days (which would be 2026-08-19).
    expect(offer('1M').range).toEqual({ from: '2026-08-18', to: '2026-09-18' });
    expect(offer('1Y').range).toEqual({ from: '2025-09-18', to: '2026-09-18' });
    expect(offer('2Y').range).toEqual({ from: '2024-09-18', to: '2026-09-18' });
  });

  it('moves a start that falls on a closed day forward to the next session, never back', () => {
    // 2026-01-01 is a holiday, so YTD starts on the 2nd; 2026-03-18 is a session and stays.
    expect(offer('YTD').range).toEqual({ from: '2026-01-02', to: '2026-09-18' });
    expect(offer('6M').range).toEqual({ from: '2026-03-18', to: '2026-09-18' });
  });

  it('refuses a range that starts before the first held day, with the reason', () => {
    expect(offer('5Y')).toEqual({ key: '5Y', range: null, unavailable: 'QQQ is held from 2024-09-03.' });
  });

  it('serves a range whose start snaps onto the first held day', () => {
    // Two years before Tuesday 2026-09-01 is Sunday 2024-09-01; Monday is Labor Day; Tuesday is the first held day.
    const bounds = heldBounds('QQQ', '2024-09-03', '2026-09-01');
    expect(offer('2Y', bounds).range).toEqual({ from: '2024-09-03', to: '2026-09-01' });
  });

  it('Max is the whole bound', () => {
    expect(offer('Max').range).toEqual({ from: '2024-09-03', to: '2026-09-18' });
  });

  it('ends on the last completed session when the page has no last day of its own', () => {
    const fetched = { ...UNBOUNDED, min: '2021-10-04', minReason: 'The provider serves bars from 2021-10-04.' };
    expect(offer('1D', fetched).range).toEqual({ from: '2026-10-01', to: '2026-10-01' });
    expect(offer('Max', fetched).range).toEqual({ from: '2021-10-04', to: '2026-10-01' });
    expect(offer('5Y', fetched).unavailable).toBe('The provider serves bars from 2021-10-04.');
  });

  it('offers nothing while the span or the calendar is unknown, and says why', () => {
    const unknown = offerQuickRanges(QUICK_RANGES, heldBounds('', null, null), SESSIONS, LAST_COMPLETED);
    expect(unknown.every((o) => o.range === null && o.unavailable === 'Pick an instrument to see the dates it is held for.')).toBe(true);

    const unread = offerQuickRanges(['1M'], QQQ, null, LAST_COMPLETED);
    expect(unread).toEqual([{ key: '1M', range: null, unavailable: 'The trading calendar has not been read.' }]);
  });
});

describe('activeQuickRange', () => {
  it('names a range only when the dates are exactly what its click would set', () => {
    const offers = offerQuickRanges(QUICK_RANGES, QQQ, SESSIONS, LAST_COMPLETED);

    expect(activeQuickRange(offers, '2026-08-18', '2026-09-18')).toBe('1M');
    expect(activeQuickRange(offers, '2026-08-19', '2026-09-18')).toBeNull();
    expect(activeQuickRange(offers, '', '')).toBeNull();
  });
});

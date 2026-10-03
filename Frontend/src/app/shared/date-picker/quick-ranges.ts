import { shiftIsoDateByMonths } from '../date/et-midnight';
import { addDays, type IsoDate } from '../date/iso-date';
import type { DateBounds } from './date-bounds';
import type { TradingSessions } from './trading-sessions';

/** Every quick range, in the order the row shows them. A page may leave some out; it never adds its own. */
export const QUICK_RANGES = ['1D', '1W', '1M', '3M', '6M', 'YTD', '1Y', '2Y', '5Y', 'Max'] as const;
export type QuickRangeKey = (typeof QUICK_RANGES)[number];

export interface DateRange {
  readonly from: IsoDate;
  readonly to: IsoDate;
}

export interface QuickRangeOffer {
  readonly key: QuickRangeKey;
  /** The dates a click sets; null when the range cannot be served. */
  readonly range: DateRange | null;
  /** Why the range cannot be served; null when it can. */
  readonly unavailable: string | null;
}

/** Where ``key`` starts when it ends on ``end``, on the calendar and before any snap; null for the whole bound. */
function calendarStart(key: QuickRangeKey, end: IsoDate): IsoDate | null {
  switch (key) {
    case '1D':
      return end;
    case '1W':
      return addDays(end, -7);
    case '1M':
      return shiftIsoDateByMonths(end, -1);
    case '3M':
      return shiftIsoDateByMonths(end, -3);
    case '6M':
      return shiftIsoDateByMonths(end, -6);
    case 'YTD':
      return `${end.slice(0, 4)}-01-01`;
    case '1Y':
      return shiftIsoDateByMonths(end, -12);
    case '2Y':
      return shiftIsoDateByMonths(end, -24);
    case '5Y':
      return shiftIsoDateByMonths(end, -60);
    case 'Max':
      return null;
  }
}

/**
 * What each quick range would set, or why it cannot be served.
 *
 * A range ends on the last day the page's run can read (``bounds.max``, else
 * the last completed session) and starts that span back on the calendar: 1M
 * is one calendar month, not thirty days. The start then moves forward to the
 * next session, so a range never reaches past its label. A range whose start
 * falls before ``bounds.min`` is refused with the bound's own reason.
 */
export function offerQuickRanges(
  keys: readonly QuickRangeKey[],
  bounds: DateBounds,
  sessions: TradingSessions | null,
  nowMs: number,
): QuickRangeOffer[] {
  const refuseAll = (reason: string): QuickRangeOffer[] => keys.map((key) => ({ key, range: null, unavailable: reason }));
  if (bounds.unknown !== null) return refuseAll(bounds.unknown);
  if (sessions === null) return refuseAll('The trading calendar has not been read.');
  const last = bounds.max ?? sessions.lastCompleted(nowMs);
  const end = last === null ? null : sessions.onOrBefore(last);
  if (end === null) return refuseAll('The trading calendar holds no session to end on.');

  return keys.map((key) => {
    const refuse = (reason: string): QuickRangeOffer => ({ key, range: null, unavailable: reason });
    const start = calendarStart(key, end) ?? bounds.min;
    if (start === null) return refuse('This page has no first day to start from.');
    if (!sessions.covers(start)) return refuse(`The trading calendar starts at ${sessions.from}.`);
    const from = sessions.onOrAfter(start);
    if (from === null || from > end) return refuse(`No trading session between ${start} and ${end}.`);
    if (bounds.min !== null && from < bounds.min) return refuse(bounds.minReason ?? `The first day is ${bounds.min}.`);
    return { key, range: { from, to: end }, unavailable: null };
  });
}

/** The quick range whose click would set exactly ``from`` to ``to``; null when the dates match none. */
export function activeQuickRange(offers: readonly QuickRangeOffer[], from: string, to: string): QuickRangeKey | null {
  return offers.find((offer) => offer.range !== null && offer.range.from === from && offer.range.to === to)?.key ?? null;
}

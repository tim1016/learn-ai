import { isoDateAfter, shiftIsoDateByMonths } from '../date/et-midnight';
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
  /** What a click sets that the label does not say; absent when the range is exactly its label. */
  readonly note?: string;
}

/** What a host's run asks of its quick ranges. */
export interface QuickRangeRules {
  /** Whether both ends must be trading sessions. */
  readonly sessionsOnly?: boolean;
  /** Whether a range must be exactly its label, as a planner that counts whole months needs: 5Y then has no leeway. */
  readonly exact?: boolean;
}

/**
 * How many days short of five years 5Y may start and still be offered.
 *
 * The provider's plan is five years, and its first day is kept two days
 * inside that. A range ends on the last completed session, which is a day
 * behind today and more after a long weekend. So an exact five years always
 * starts a few days before anything the provider serves, and before anything
 * the lake could have fetched from it.
 */
const FIVE_YEAR_LEEWAY_DAYS = 7;

/** The day ``months`` whole calendar months before the day after ``end``: the first day of a range of exactly that many months. */
function monthsBefore(end: IsoDate, months: number): IsoDate {
  return shiftIsoDateByMonths(isoDateAfter(end), -months);
}

/** Where ``key`` starts when it ends on ``end``, on the calendar and before any snap; null for the whole bound. */
function calendarStart(key: QuickRangeKey, end: IsoDate): IsoDate | null {
  switch (key) {
    case '1D':
      return end;
    case '1W':
      return addDays(end, -6);
    case '1M':
      return monthsBefore(end, 1);
    case '3M':
      return monthsBefore(end, 3);
    case '6M':
      return monthsBefore(end, 6);
    case 'YTD':
      return `${end.slice(0, 4)}-01-01`;
    case '1Y':
      return monthsBefore(end, 12);
    case '2Y':
      return monthsBefore(end, 24);
    case '5Y':
      return monthsBefore(end, 60);
    case 'Max':
      return null;
  }
}

/**
 * What each quick range would set, or why it cannot be served.
 *
 * A range ends on the last day the page's run can read (``bounds.max``, else
 * ``lastCompleted``, the last session that has closed) and is exactly its
 * label on the calendar, both ends included: 1Y ending 2026-09-18 starts
 * 2025-09-19, and 1M is one calendar month, not thirty days. That is a whole
 * number of months as the fold planners count them, except where the start's
 * month is shorter than the day asked for (6M ending 2026-10-30 starts
 * 2026-04-30, a day more than six months); a planner refuses such a range in
 * its own words. With ``sessionsOnly`` the start then moves forward to the
 * next session, so a range never reaches past its label. A range whose start falls before ``bounds.min`` is refused with the
 * bound's own reason. 5Y is the exception: with its calendar start up to
 * ``FIVE_YEAR_LEEWAY_DAYS`` before ``bounds.min``, it starts on ``bounds.min``
 * and carries a note saying so, unless the host's ranges must be ``exact``.
 */
export function offerQuickRanges(
  keys: readonly QuickRangeKey[],
  bounds: DateBounds,
  sessions: TradingSessions | null,
  lastCompleted: IsoDate | null,
  { sessionsOnly = true, exact = false }: QuickRangeRules = {},
): QuickRangeOffer[] {
  const refuseAll = (reason: string): QuickRangeOffer[] => keys.map((key) => ({ key, range: null, unavailable: reason }));
  if (bounds.unknown !== null) return refuseAll(bounds.unknown);
  if (sessions === null) return refuseAll('The trading calendar has not been read.');
  const last = bounds.max ?? lastCompleted;
  const end = last === null ? null : sessions.onOrBefore(last);
  if (end === null) return refuseAll('The trading calendar holds no session to end on.');

  return keys.map((key) => {
    const refuse = (reason: string): QuickRangeOffer => ({ key, range: null, unavailable: reason });
    const start = calendarStart(key, end) ?? bounds.min;
    if (start === null) return refuse('This page has no first day to start from.');
    if (!sessions.covers(start)) return refuse(`The trading calendar starts at ${sessions.from}.`);
    const from = sessionsOnly ? sessions.onOrAfter(start) : start;
    if (from === null || from > end) return refuse(`No trading session between ${start} and ${end}.`);
    if (bounds.min === null || from >= bounds.min) return { key, range: { from, to: end }, unavailable: null };
    const reason = bounds.minReason ?? `The first day is ${bounds.min}.`;
    if (key !== '5Y' || exact || start < addDays(bounds.min, -FIVE_YEAR_LEEWAY_DAYS)) return refuse(reason);
    const first = sessionsOnly ? sessions.onOrAfter(bounds.min) : bounds.min;
    if (first === null || first > end) return refuse(reason);
    return { key, range: { from: first, to: end }, unavailable: null, note: `Starts on ${first}. ${reason}` };
  });
}

/** Every quick range whose click would set exactly ``from`` to ``to``: 5Y and Max are the same dates where a page reads just short of five years. */
export function activeQuickRanges(offers: readonly QuickRangeOffer[], from: string, to: string): ReadonlySet<QuickRangeKey> {
  return new Set(offers.filter((offer) => offer.range !== null && offer.range.from === from && offer.range.to === to).map((offer) => offer.key));
}

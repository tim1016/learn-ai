import { isWeekend, parseIsoDate, type IsoDate } from '../date/iso-date';
import type { DateBounds } from './date-bounds';
import type { TradingSessions } from './trading-sessions';

/** What a typed date is checked against. */
export interface DateCheckContext {
  readonly bounds: DateBounds;
  /** The calendar, when the date must be a trading session; null leaves the day unchecked. */
  readonly sessions: TradingSessions | null;
}

/** The date a field's text stands for, or why it stands for none. Empty text is empty, not a problem. */
export interface DateCheck {
  /** The usable date; empty when the text is empty or refused. */
  readonly date: IsoDate | '';
  readonly problem: string | null;
}

const USABLE = (date: IsoDate): DateCheck => ({ date, problem: null });
const REFUSED = (problem: string): DateCheck => ({ date: '', problem });

function closedDay(date: IsoDate, sessions: TradingSessions): string {
  const holiday = sessions.holidayName(date);
  const why = holiday !== null ? `a market holiday (${holiday})` : isWeekend(date) ? 'a weekend' : 'not a trading day';
  const next = sessions.onOrAfter(date);
  return `${date} is ${why}.${next === null ? '' : ` The next session is ${next}.`}`;
}

/**
 * Reads one typed date. Nothing is corrected: text that is not a whole
 * ``YYYY-MM-DD`` date, falls outside the bounds, or names a closed day is
 * refused with the reason, and the caller hands its host an empty date.
 */
export function checkDate(text: string, context: DateCheckContext): DateCheck {
  if (text === '') return { date: '', problem: null };
  const date = parseIsoDate(text);
  if (date === null) return REFUSED('Enter a date as YYYY-MM-DD.');
  const { bounds, sessions } = context;
  if (bounds.min !== null && date < bounds.min) return REFUSED(bounds.minReason ?? `The first day is ${bounds.min}.`);
  if (bounds.max !== null && date > bounds.max) return REFUSED(bounds.maxReason ?? `The last day is ${bounds.max}.`);
  if (sessions !== null) {
    if (!sessions.covers(date)) return REFUSED(`The trading calendar runs from ${sessions.from} to ${sessions.through}.`);
    if (!sessions.isSession(date)) return REFUSED(closedDay(date, sessions));
  }
  return USABLE(date);
}

/** Reads a typed range: each end on its own, then their order. */
export function checkRange(fromText: string, toText: string, context: DateCheckContext): { from: DateCheck; to: DateCheck } {
  const from = checkDate(fromText, context);
  const to = checkDate(toText, context);
  if (from.date !== '' && to.date !== '' && from.date > to.date) {
    return { from: REFUSED('The start date is after the end date.'), to };
  }
  return { from, to };
}

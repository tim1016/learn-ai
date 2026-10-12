import { etMidnightMs } from '../../../shared/date/et-midnight';
import { formatTimestampDisplay } from '../../../shared/timestamp';
import { MS_PER_MINUTE, parseClockText } from '../session-time';
import type { SessionBounds } from './past-chain.types';

/**
 * Turning what the owner typed into the instants the routes take. A trading date is New York midnight (the routes ask
 * for exactly that); a minute of its session is that midnight plus the minute. A trading day has no clock change in it
 * (they happen on Sundays), so the sum is the instant. The session's own open and close come from the answer for the
 * date, so an early close is respected and no session time is written here.
 */

/** The trading date as the routes take it, or null while the field holds no usable date. */
export function tradingDateMsOf(date: string): number | null {
  return date === '' ? null : etMidnightMs(date);
}

/** What the time box says: nothing (the server's default, the session's close), a minute of the session, or a refusal. */
export type ChosenMinute =
  | { readonly kind: 'close' }
  /** A minute is typed but there is no date to put it on yet. */
  | { readonly kind: 'waiting' }
  | { readonly kind: 'at'; readonly atMs: number }
  | { readonly kind: 'refused'; readonly problem: string };

/** The instant for the `HH:MM` typed in the time box, on the New York date whose midnight is `tradingDateMs`. */
export function chosenMinute(text: string, tradingDateMs: number | null, session: SessionBounds | null): ChosenMinute {
  const typed = text.trim();
  if (typed === '') return { kind: 'close' };
  const minute = parseClockText(typed);
  if (minute === null) return { kind: 'refused', problem: 'Enter a time as hours and minutes.' };
  if (tradingDateMs === null) return { kind: 'waiting' };
  const atMs = tradingDateMs + minute * MS_PER_MINUTE;
  if (session === null) return { kind: 'at', atMs };
  const range = `Enter a time from ${clockOf(session.openMs)} to ${clockOf(session.closeMs)}.`;
  if (atMs < session.openMs) return { kind: 'refused', problem: `That is before the open. ${range}` };
  if (atMs > session.closeMs) return { kind: 'refused', problem: `That is after the close. ${range}` };
  return { kind: 'at', atMs };
}

/** An instant as the New York clock minute, through the shared display. */
function clockOf(ms: number): string {
  return formatTimestampDisplay(ms, { mode: 'et', granularity: 'minute' });
}

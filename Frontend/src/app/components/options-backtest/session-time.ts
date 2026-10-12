import type { Anchor, SessionClock, SessionTimeWire } from './options-backtest.types';

export const MS_PER_MINUTE = 60_000;
/** Noon New York, minutes past midnight: a "No re-entry after" at or before it is stored from the open, a later one from the close. */
export const NOON_MINUTE_ET = 12 * 60;
const MINUTES_PER_DAY = 24 * 60;

/** The three times a setup holds, which differ in where they are stored from. */
export type TimeRole = 'entry' | 'exit' | 'noReentry';

/**
 * The least distance, in minutes, the setup schema allows an entry or an exit
 * at its anchor, mirrored from `setup.py` (`_the_window_can_hold_a_trade`): an
 * order fills in the minute after the one that sets it off, so an entry is a
 * minute after the open and an exit a minute before the close; the exit comes
 * after the entry, so an entry from the close needs two minutes and so does an
 * exit from the open. "No re-entry after" has no such rule. The server still
 * judges every body; this says the refusal at the field it belongs to.
 */
const LEAST_MINUTES: Record<'entry' | 'exit', Record<Anchor, number>> = {
  entry: { open: 1, close: 2 },
  exit: { open: 2, close: 1 },
};

export type StoredTime =
  | { readonly ok: true; readonly time: SessionTimeWire }
  | { readonly ok: false; readonly problem: string };

/**
 * A setup stores a time as a distance from the open or before the close, so a
 * half day keeps its shape; the owner types a New York clock time. This is the
 * one place the two meet. The clock is the full session the server serves: no
 * session time is written here.
 */

/** `HH:MM` (what a time input holds) as minutes past New York midnight, or null when it is not a whole minute of a day. */
export function parseClockText(text: string): number | null {
  const match = /^(\d{2}):(\d{2})$/.exec(text);
  if (match === null) return null;
  const hours = Number(match[1]);
  const minutes = Number(match[2]);
  return hours < 24 && minutes < 60 ? hours * 60 + minutes : null;
}

export function formatClockText(minute: number): string {
  return `${String(Math.floor(minute / 60)).padStart(2, '0')}:${String(minute % 60).padStart(2, '0')}`;
}

/** Entry is stored from the open, exit from the close, and "No re-entry after" by the noon rule. */
export function anchorFor(role: TimeRole, minute: number): Anchor {
  if (role === 'entry') return 'open';
  if (role === 'exit') return 'close';
  return minute <= NOON_MINUTE_ET ? 'open' : 'close';
}

/**
 * The stored form of a typed time. A time outside the full session, both ends
 * inside it, is a problem for the field to show, as is text that is not a
 * whole minute, and so is an entry or an exit the schema would refuse for
 * being too close to the end it is counted from. `held` is the anchor a loaded
 * setup came with, kept so that reopening a setup does not change what it means.
 */
export function storedTime(role: TimeRole, text: string, clock: SessionClock, held?: Anchor): StoredTime {
  const minute = parseClockText(text);
  if (minute === null) return { ok: false, problem: 'Enter a time as hours and minutes.' };
  const { open_minute_et: open, close_minute_et: close } = clock;
  const range = `Enter a time from ${formatClockText(open)} to ${formatClockText(close)}.`;
  if (minute < open) return { ok: false, problem: `That is before the open. ${range}` };
  if (minute > close) return { ok: false, problem: `That is after the close. ${range}` };
  const anchor = held ?? anchorFor(role, minute);
  const distance = anchor === 'open' ? minute - open : close - minute;
  if (role !== 'noReentry' && distance < LEAST_MINUTES[role][anchor]) {
    return { ok: false, problem: tooCloseToTheEnd(role, anchor, clock) };
  }
  return { ok: true, time: { anchor, offset_ms: distance * MS_PER_MINUTE } };
}

/** The refusal of a time too close to the end it is counted from, with the nearest time that is allowed. */
function tooCloseToTheEnd(role: 'entry' | 'exit', anchor: Anchor, clock: SessionClock): string {
  const least = LEAST_MINUTES[role][anchor];
  const amount = least === 1 ? 'a minute' : `${least} minutes`;
  // Only a distance of two minutes has a reason beyond the fill: the exit has to come after the entry.
  const reason = least === 1 ? '' : role === 'entry' ? ', to leave room for the exit' : ', to come after an entry';
  const subject = role === 'entry' ? 'The entry' : 'The exit';
  if (anchor === 'open') {
    return `${subject} must be at least ${amount} after the open${reason}: ${formatClockText(clock.open_minute_et + least)} or later.`;
  }
  return `${subject} must be at least ${amount} before the close${reason}: ${formatClockText(clock.close_minute_et - least)} or earlier.`;
}

/**
 * The exit must be after the entry. When the two are counted from the same end
 * of the day that is known from the stored times alone, so it is said at the
 * exit; an entry from the open against an exit from the close depends on how
 * long the session is, which the server's summary tells.
 */
export function exitAfterEntry(entry: SessionTimeWire, exit: SessionTimeWire, clock: SessionClock): string | null {
  if (entry.anchor !== exit.anchor) return null;
  const later = entry.anchor === 'open' ? exit.offset_ms > entry.offset_ms : exit.offset_ms < entry.offset_ms;
  if (later) return null;
  const at = clockTextOf(entry, clock);
  return at === '' ? 'The exit must be after the entry.' : `The exit must be after the entry, which is at ${at}.`;
}

/** The clock time a stored distance names on a full session, or '' when it names no time of one day. */
export function clockTextOf(time: SessionTimeWire, clock: SessionClock): string {
  const distance = time.offset_ms / MS_PER_MINUTE;
  if (!Number.isInteger(distance)) return '';
  const minute = time.anchor === 'open' ? clock.open_minute_et + distance : clock.close_minute_et - distance;
  return minute >= 0 && minute < MINUTES_PER_DAY ? formatClockText(minute) : '';
}

/** What is stored, in words: "5 minutes after the open". */
export function distanceInWords(time: SessionTimeWire): string {
  const minutes = time.offset_ms / MS_PER_MINUTE;
  const where = time.anchor === 'open' ? 'the open' : 'the close';
  if (minutes === 0) return `at ${where}`;
  const count = `${minutes} ${minutes === 1 ? 'minute' : 'minutes'}`;
  return time.anchor === 'open' ? `${count} after ${where}` : `${count} before ${where}`;
}

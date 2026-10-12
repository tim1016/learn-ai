import type { Anchor, SessionClock, SessionTimeWire } from './options-backtest.types';

export const MS_PER_MINUTE = 60_000;
/** Noon New York, minutes past midnight: a "No re-entry after" at or before it is stored from the open, a later one from the close. */
export const NOON_MINUTE_ET = 12 * 60;
const MINUTES_PER_DAY = 24 * 60;

/** The three times a setup holds, which differ in where they are stored from. */
export type TimeRole = 'entry' | 'exit' | 'noReentry';

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
 * whole minute. `held` is the anchor a loaded setup came with, kept so that
 * reopening a setup does not change what it means.
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
  return { ok: true, time: { anchor, offset_ms: distance * MS_PER_MINUTE } };
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

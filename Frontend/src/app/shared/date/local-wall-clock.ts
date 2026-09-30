/**
 * The viewer's own wall clock as a date input and a time input hold it, and
 * back to ``int64 ms UTC``.
 *
 * A time the owner types is typed in the zone they look at (their local
 * zone), so it is converted from its numeric parts, never by parsing a
 * string as a date (temporal-rigor.md bans ``new Date(string)``). The
 * ``Date`` here is arithmetic inside one function only; nothing but the ms
 * value leaves.
 */

/** ``YYYY-MM-DD`` and ``HH:MM``, the values `<input type="date">` and `<input type="time">` carry. */
export interface LocalWallClock {
  readonly date: string;
  readonly clock: string;
}

const DATE_RE = /^(\d{4})-(\d{2})-(\d{2})$/;
const CLOCK_RE = /^(\d{2}):(\d{2})$/;

const two = (value: number): string => String(value).padStart(2, '0');

/** The viewer's local date and minute that contain ``ms``. */
export function localWallClock(ms: number): LocalWallClock {
  const local = new Date(ms);
  return {
    date: `${local.getFullYear()}-${two(local.getMonth() + 1)}-${two(local.getDate())}`,
    clock: `${two(local.getHours())}:${two(local.getMinutes())}`,
  };
}

/** Whether ``date`` is a real ``YYYY-MM-DD`` calendar day (never a 31st of a 30-day month). */
export function isCalendarDate(date: string): boolean {
  const parts = DATE_RE.exec(date);
  if (parts === null) return false;
  const [year, month, day] = [parts[1], parts[2], parts[3]].map(Number);
  const utc = new Date(Date.UTC(year, month - 1, day));
  return utc.getUTCFullYear() === year && utc.getUTCMonth() === month - 1 && utc.getUTCDate() === day;
}

/** Whether ``clock`` is a real ``HH:MM`` minute of a day. */
export function isClockMinute(clock: string): boolean {
  const parts = CLOCK_RE.exec(clock);
  return parts !== null && Number(parts[1]) < 24 && Number(parts[2]) < 60;
}

/**
 * The instant the viewer means by ``wall`` in their local zone, or `null`
 * when either half is blank, malformed, or names a wall clock that does not
 * exist there (a 31st of a 30-day month, or the hour a spring-forward skips).
 */
export function localWallClockMs(wall: LocalWallClock): number | null {
  const date = DATE_RE.exec(wall.date);
  const clock = CLOCK_RE.exec(wall.clock);
  if (date === null || clock === null) return null;
  const [year, month, day, hour, minute] = [date[1], date[2], date[3], clock[1], clock[2]].map(Number);
  const ms = new Date(year, month - 1, day, hour, minute).getTime();
  if (!Number.isFinite(ms)) return null;
  const roundTrip = localWallClock(ms);
  return roundTrip.date === wall.date && roundTrip.clock === wall.clock ? ms : null;
}

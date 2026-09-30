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
  readonly time: string;
}

const DATE_RE = /^(\d{4})-(\d{2})-(\d{2})$/;
const TIME_RE = /^(\d{2}):(\d{2})$/;

const two = (value: number): string => String(value).padStart(2, '0');

/** The viewer's local date and minute that contain ``ms``. */
export function localWallClock(ms: number): LocalWallClock {
  const local = new Date(ms);
  return {
    date: `${local.getFullYear()}-${two(local.getMonth() + 1)}-${two(local.getDate())}`,
    time: `${two(local.getHours())}:${two(local.getMinutes())}`,
  };
}

/**
 * The instant the viewer means by ``clock`` in their local zone, or `null`
 * when either half is blank, malformed, or names a wall clock that does not
 * exist there (a 31st of a 30-day month, or the hour a spring-forward skips).
 */
export function localWallClockMs(clock: LocalWallClock): number | null {
  const date = DATE_RE.exec(clock.date);
  const time = TIME_RE.exec(clock.time);
  if (date === null || time === null) return null;
  const [year, month, day, hour, minute] = [date[1], date[2], date[3], time[1], time[2]].map(Number);
  const ms = new Date(year, month - 1, day, hour, minute).getTime();
  if (!Number.isFinite(ms)) return null;
  const roundTrip = localWallClock(ms);
  return roundTrip.date === clock.date && roundTrip.time === clock.time ? ms : null;
}

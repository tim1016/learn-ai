/**
 * ET-anchored trading-date boundaries as ``int64 ms UTC``.
 *
 * A trading date on the wire is one ms value anchored at an ET session
 * boundary, never a string (temporal-rigor.md, "Date-anchored and wall-clock
 * values"). The client picks dates in a date input, so it needs the exact
 * instant "midnight America/New_York on that date" — which moves against UTC
 * across DST. Resolved through ``Intl`` so no fixed offset is ever assumed.
 */

const ET_ZONE = 'America/New_York';
const MS_PER_DAY = 24 * 60 * 60 * 1000;

const ET_PARTS = new Intl.DateTimeFormat('en-US', {
  timeZone: ET_ZONE,
  year: 'numeric',
  month: '2-digit',
  day: '2-digit',
  hour: '2-digit',
  minute: '2-digit',
  hourCycle: 'h23',
});

function etWallClockAsUtc(ms: number): number {
  const parts: Partial<Record<Intl.DateTimeFormatPartTypes, string>> = {};
  for (const part of ET_PARTS.formatToParts(new Date(ms))) {
    if (part.type !== 'literal') parts[part.type] = part.value;
  }
  const hour = parts.hour === '24' ? 0 : Number(parts.hour);
  return Date.UTC(Number(parts.year), Number(parts.month) - 1, Number(parts.day), hour, Number(parts.minute));
}

/** ``YYYY-MM-DD`` → the ms instant of ET midnight on that calendar date. */
export function etMidnightMs(isoDate: string): number {
  const match = /^(\d{4})-(\d{2})-(\d{2})$/.exec(isoDate);
  if (match === null) throw new Error(`expected YYYY-MM-DD, got ${isoDate}`);
  const wanted = Date.UTC(Number(match[1]), Number(match[2]) - 1, Number(match[3]));
  // Guess the instant as if ET were UTC-5, then correct by however far the
  // ET wall clock at that guess sits from the midnight we want. One
  // correction suffices because ET's offset changes only at 02:00 local.
  let guess = wanted + 5 * 60 * 60 * 1000;
  guess -= etWallClockAsUtc(guess) - wanted;
  return guess;
}

/** ``YYYY-MM-DD`` → the ms instant of ET midnight the day AFTER, i.e. the half-open end of that date. */
export function etDayEndMs(isoDate: string): number {
  return etMidnightMs(isoDateAfter(isoDate));
}

/** The ET calendar date (``YYYY-MM-DD``) that contains ``ms``. */
export function etIsoDate(ms: number): string {
  const wall = new Date(etWallClockAsUtc(ms));
  return wall.toISOString().slice(0, 10);
}

/** The calendar date after ``isoDate`` (``YYYY-MM-DD`` arithmetic, no zone involved). */
export function isoDateAfter(isoDate: string): string {
  const [y, m, d] = isoDate.split('-').map(Number);
  return new Date(Date.UTC(y, m - 1, d) + MS_PER_DAY).toISOString().slice(0, 10);
}

/** ``isoDate`` moved by whole calendar months, the day clamped to the target month's length (as the fold planner does). */
export function shiftIsoDateByMonths(isoDate: string, months: number): string {
  const [y, m, d] = isoDate.split('-').map(Number);
  const first = new Date(Date.UTC(y, m - 1 + months, 1));
  const lastDay = new Date(Date.UTC(first.getUTCFullYear(), first.getUTCMonth() + 1, 0)).getUTCDate();
  return new Date(Date.UTC(first.getUTCFullYear(), first.getUTCMonth(), Math.min(d, lastDay))).toISOString().slice(0, 10);
}

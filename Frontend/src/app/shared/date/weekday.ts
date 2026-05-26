/**
 * Walks a date back to the most recent Mon-Fri, in the same calendar
 * day semantics as the caller's local timezone.
 *
 * Used by every place in the engine-lab and ticker-range-picker stack
 * that derives a date from arithmetic (``today - N days``,
 * ``ticker.last - 30``) and feeds it to a server endpoint that rejects
 * weekends. Holidays are NOT handled here — server still returns 422
 * for those, by design, so they surface visibly to the user instead of
 * being silently bumped.
 *
 * Pure, no side effects. Returns a new ``Date`` and never mutates ``d``.
 */
export function toMostRecentWeekday(d: Date): Date {
  const out = new Date(d);
  while (out.getDay() === 0 || out.getDay() === 6) {
    out.setDate(out.getDate() - 1);
  }
  return out;
}

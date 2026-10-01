/** Polygon.io Starter plan limits: 2 years of historical data, 15-min delayed. */

/** Returns the earliest allowed date string (YYYY-MM-DD) — 2 years ago from today. */
export function getMinAllowedDate(): string {
  const d = new Date();
  d.setFullYear(d.getFullYear() - 2);
  return d.toISOString().split('T')[0];
}

/** Today as a YYYY-MM-DD string in UTC. */
export function todayDateString(): string {
  return new Date().toISOString().slice(0, 10);
}

/**
 * Today + N months as a YYYY-MM-DD string in UTC. Uses calendar arithmetic
 * (Date.setMonth) so it is DST-safe — `Date.now() + N * 86_400_000` is not.
 */
export function dateStringMonthsFromNow(months: number): string {
  const d = new Date();
  d.setMonth(d.getMonth() + months);
  return d.toISOString().slice(0, 10);
}

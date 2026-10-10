/** Polygon.io Starter plan limits: 2 years of historical data, 15-min delayed. */

/** Returns the earliest allowed date string (YYYY-MM-DD) — 2 years ago from today. */
export function getMinAllowedDate(): string {
  const d = new Date();
  d.setFullYear(d.getFullYear() - 2);
  return d.toISOString().split('T')[0];
}


/** Polygon.io Starter plan limits: 2 years of historical data, 15-min delayed. */

/** Returns the earliest allowed date string (YYYY-MM-DD) — 2 years ago from today. */
export function getMinAllowedDate(): string {
  const d = new Date();
  d.setFullYear(d.getFullYear() - 2);
  return d.toISOString().split('T')[0];
}

/**
 * Validates that a date is within the Polygon 2-year data window.
 * Returns an error message if invalid, or null if valid.
 */
export function validateDateRange(fromDate: string, toDate: string): string | null {
  const minDate = getMinAllowedDate();
  if (fromDate < minDate) {
    return `From date exceeds the 2-year historical data limit (Polygon Starter plan). Earliest allowed: ${minDate}`;
  }
  if (toDate < minDate) {
    return `To date exceeds the 2-year historical data limit (Polygon Starter plan). Earliest allowed: ${minDate}`;
  }
  if (fromDate > toDate) {
    return 'From date must be before To date.';
  }
  return null;
}

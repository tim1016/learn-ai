/** Polygon.io Starter plan limits: 2 years of historical data, 15-min delayed. */

import { MarketHolidayEvent } from '../models/market-monitor';

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

// ---------------------------------------------------------------------------
// Market holiday / trading-day utilities
// ---------------------------------------------------------------------------

/** Returns the holiday event if the date is a market holiday, or null. */
export function isMarketHoliday(
  date: Date,
  holidays: MarketHolidayEvent[]
): MarketHolidayEvent | null {
  const dateStr = formatDateStr(date);
  return holidays.find(h => h.date === dateStr) ?? null;
}

/** Returns true if the date is a non-trading day (weekend or closed holiday). */
export function isNonTradingDay(
  date: Date,
  holidays: MarketHolidayEvent[]
): boolean {
  const day = date.getDay();
  if (day === 0 || day === 6) return true;
  const holiday = isMarketHoliday(date, holidays);
  return holiday !== null && holiday.status === 'Closed';
}

/** Converts holidays into a Date[] for PrimeNG's [disabledDates] (only fully closed days). */
export function getDisabledHolidayDates(
  holidays: MarketHolidayEvent[]
): Date[] {
  return holidays
    .filter(h => h.date && h.status === 'Closed')
    .map(h => new Date(h.date + 'T00:00:00'));
}

/** Builds a fast lookup map from 'YYYY-MM-DD' -> MarketHolidayEvent. */
export function buildHolidayMap(
  holidays: MarketHolidayEvent[]
): Map<string, MarketHolidayEvent> {
  const map = new Map<string, MarketHolidayEvent>();
  for (const h of holidays) {
    if (h.date) {
      map.set(h.date, h);
    }
  }
  return map;
}

function formatDateStr(date: Date): string {
  const y = date.getFullYear();
  const m = String(date.getMonth() + 1).padStart(2, '0');
  const d = String(date.getDate()).padStart(2, '0');
  return `${y}-${m}-${d}`;
}

// ---------------------------------------------------------------------------
// YYYY-MM-DD <-> Date adapters (canonical conversion for UI <-> wire boundary)
// ---------------------------------------------------------------------------

/**
 * Parse a strict 'YYYY-MM-DD' string to a Date at local midnight.
 * Returns null for empty input, non-matching format (including
 * single-digit month/day like '2025-5-31'), or impossible calendar
 * dates like '2025-02-30'.
 */
export function parseYmd(s: string): Date | null {
  if (!/^\d{4}-\d{2}-\d{2}$/.test(s)) return null;
  const [y, m, d] = s.split('-').map(Number);
  const date = new Date(y, m - 1, d, 0, 0, 0, 0);
  if (date.getFullYear() !== y || date.getMonth() !== m - 1 || date.getDate() !== d) {
    return null;
  }
  return date;
}

/**
 * Format a Date as 'YYYY-MM-DD' in local time, zero-padded.
 * Returns '' for null. Inverse of parseYmd.
 */
export function formatYmd(d: Date | null): string {
  if (d === null) return '';
  const y = d.getFullYear();
  const m = String(d.getMonth() + 1).padStart(2, '0');
  const day = String(d.getDate()).padStart(2, '0');
  return `${y}-${m}-${day}`;
}

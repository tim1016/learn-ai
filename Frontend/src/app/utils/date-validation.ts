/** Polygon.io Starter plan limits: 2 years of historical data, 15-min delayed. */

import { MarketHolidayEvent } from '../models/market-monitor';

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

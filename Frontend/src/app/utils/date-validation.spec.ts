/**
 * Date validation utilities — unit tests.
 */
import {
  getMinAllowedDate,
  validateDateRange,
  isMarketHoliday,
  isNonTradingDay,
  getDisabledHolidayDates,
  buildHolidayMap,
} from './date-validation';
import { MarketHolidayEvent } from '../models/market-monitor';

// ---------------------------------------------------------------------------
// Test fixtures
// ---------------------------------------------------------------------------

const holidays: MarketHolidayEvent[] = [
  { date: '2026-01-01', exchanges: ['NYSE'], name: "New Year's Day", status: 'Closed', open: null, close: null },
  { date: '2026-01-19', exchanges: ['NYSE'], name: 'MLK Day', status: 'Closed', open: null, close: null },
  { date: '2026-07-03', exchanges: ['NYSE'], name: 'Independence Day (early close)', status: 'Early Close', open: '09:30', close: '13:00' },
  { date: '2026-12-25', exchanges: ['NYSE'], name: 'Christmas Day', status: 'Closed', open: null, close: null },
];

// ---------------------------------------------------------------------------
// getMinAllowedDate
// ---------------------------------------------------------------------------

describe('getMinAllowedDate', () => {
  it('should return a date 2 years in the past', () => {
    const result = getMinAllowedDate();
    const expected = new Date();
    expected.setFullYear(expected.getFullYear() - 2);
    const expectedStr = expected.toISOString().split('T')[0];
    expect(result).toBe(expectedStr);
  });

  it('should return a valid YYYY-MM-DD string', () => {
    expect(getMinAllowedDate()).toMatch(/^\d{4}-\d{2}-\d{2}$/);
  });
});

// ---------------------------------------------------------------------------
// validateDateRange
// ---------------------------------------------------------------------------

describe('validateDateRange', () => {
  it('should return null for valid date range', () => {
    const today = new Date().toISOString().split('T')[0];
    const yesterday = new Date(Date.now() - 86400000).toISOString().split('T')[0];
    expect(validateDateRange(yesterday, today)).toBeNull();
  });

  it('should return error when fromDate > toDate', () => {
    const result = validateDateRange('2026-02-01', '2026-01-01');
    expect(result).toContain('From date must be before To date');
  });

  it('should return error when fromDate exceeds 2-year limit', () => {
    const result = validateDateRange('2020-01-01', '2026-01-01');
    expect(result).toContain('2-year historical data limit');
    expect(result).toContain('From date');
  });

  it('should return error when toDate exceeds 2-year limit', () => {
    const result = validateDateRange('2020-01-01', '2020-06-01');
    expect(result).not.toBeNull();
  });

  it('should return null when both dates are at the boundary', () => {
    const minDate = getMinAllowedDate();
    expect(validateDateRange(minDate, minDate)).toBeNull();
  });
});

// ---------------------------------------------------------------------------
// isMarketHoliday
// ---------------------------------------------------------------------------

describe('isMarketHoliday', () => {
  it('should return the holiday event for a known holiday', () => {
    const result = isMarketHoliday(new Date(2026, 0, 1), holidays); // Jan 1
    expect(result).not.toBeNull();
    expect(result!.name).toBe("New Year's Day");
    expect(result!.status).toBe('Closed');
  });

  it('should return null for a non-holiday', () => {
    const result = isMarketHoliday(new Date(2026, 0, 5), holidays); // Jan 5
    expect(result).toBeNull();
  });

  it('should match early close holidays', () => {
    const result = isMarketHoliday(new Date(2026, 6, 3), holidays); // Jul 3
    expect(result).not.toBeNull();
    expect(result!.status).toBe('Early Close');
  });
});

// ---------------------------------------------------------------------------
// isNonTradingDay
// ---------------------------------------------------------------------------

describe('isNonTradingDay', () => {
  it('should return true for Saturday', () => {
    // Find a Saturday: Feb 28, 2026 is a Saturday
    const saturday = new Date(2026, 1, 28);
    expect(saturday.getDay()).toBe(6); // verify it's Saturday
    expect(isNonTradingDay(saturday, holidays)).toBe(true);
  });

  it('should return true for Sunday', () => {
    const sunday = new Date(2026, 2, 1);
    expect(sunday.getDay()).toBe(0); // verify it's Sunday
    expect(isNonTradingDay(sunday, holidays)).toBe(true);
  });

  it('should return true for a closed holiday', () => {
    const xmas = new Date(2026, 11, 25); // Dec 25
    expect(isNonTradingDay(xmas, holidays)).toBe(true);
  });

  it('should return false for an early close day (still a trading day)', () => {
    const earlyClose = new Date(2026, 6, 3); // Jul 3
    // Jul 3, 2026 is a Friday (trading day with early close)
    expect(isNonTradingDay(earlyClose, holidays)).toBe(false);
  });

  it('should return false for a regular weekday', () => {
    const tuesday = new Date(2026, 1, 3); // Feb 3 (Tuesday)
    expect(tuesday.getDay()).toBe(2);
    expect(isNonTradingDay(tuesday, holidays)).toBe(false);
  });
});

// ---------------------------------------------------------------------------
// getDisabledHolidayDates
// ---------------------------------------------------------------------------

describe('getDisabledHolidayDates', () => {
  it('should only return dates for Closed holidays (not Early Close)', () => {
    const disabled = getDisabledHolidayDates(holidays);
    // 3 Closed holidays in our fixture, 1 Early Close
    expect(disabled.length).toBe(3);
  });

  it('should return Date objects', () => {
    const disabled = getDisabledHolidayDates(holidays);
    disabled.forEach(d => expect(d).toBeInstanceOf(Date));
  });

  it('should return empty array for no holidays', () => {
    expect(getDisabledHolidayDates([])).toEqual([]);
  });
});

// ---------------------------------------------------------------------------
// buildHolidayMap
// ---------------------------------------------------------------------------

describe('buildHolidayMap', () => {
  it('should build a map keyed by date string', () => {
    const map = buildHolidayMap(holidays);
    expect(map.size).toBe(4);
    expect(map.get('2026-01-01')?.name).toBe("New Year's Day");
  });

  it('should return empty map for empty input', () => {
    expect(buildHolidayMap([]).size).toBe(0);
  });

  it('should skip holidays with no date', () => {
    const withMissing: MarketHolidayEvent[] = [
      { date: '', exchanges: ['NYSE'], name: 'Mystery', status: 'Closed', open: null, close: null },
    ];
    expect(buildHolidayMap(withMissing).size).toBe(0);
  });
});

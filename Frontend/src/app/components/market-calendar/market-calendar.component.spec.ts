import { MarketHolidayEvent } from '../../models/market-monitor';
import {
  isMarketHoliday,
  isNonTradingDay,
  getDisabledHolidayDates,
  buildHolidayMap,
} from '../../utils/date-validation';

const MOCK_HOLIDAYS: MarketHolidayEvent[] = [
  {
    date: '2026-07-03',
    name: 'Independence Day',
    status: 'Closed',
    open: null,
    close: null,
    exchanges: ['NYSE', 'NASDAQ', 'OTC'],
  },
  {
    date: '2026-11-27',
    name: 'Thanksgiving Day',
    status: 'Early Close',
    open: '2026-11-27T09:30:00-05:00',
    close: '2026-11-27T13:00:00-05:00',
    exchanges: ['NYSE', 'NASDAQ'],
  },
  {
    date: '2026-12-25',
    name: 'Christmas Day',
    status: 'Closed',
    open: null,
    close: null,
    exchanges: ['NYSE', 'NASDAQ', 'OTC'],
  },
];

describe('Date Validation - Holiday Utilities', () => {
  describe('isMarketHoliday', () => {
    it('should return the holiday event for a matching date', () => {
      const result = isMarketHoliday(new Date(2026, 6, 3), MOCK_HOLIDAYS);
      expect(result).not.toBeNull();
      expect(result!.name).toBe('Independence Day');
      expect(result!.status).toBe('Closed');
    });

    it('should return null for a non-holiday date', () => {
      const result = isMarketHoliday(new Date(2026, 6, 2), MOCK_HOLIDAYS);
      expect(result).toBeNull();
    });

    it('should return the early-close event', () => {
      const result = isMarketHoliday(new Date(2026, 10, 27), MOCK_HOLIDAYS);
      expect(result).not.toBeNull();
      expect(result!.status).toBe('Early Close');
    });

    it('should handle empty holidays list', () => {
      const result = isMarketHoliday(new Date(2026, 6, 3), []);
      expect(result).toBeNull();
    });
  });

  describe('isNonTradingDay', () => {
    it('should return true for Saturday', () => {
      // 2026-07-04 is a Saturday
      expect(isNonTradingDay(new Date(2026, 6, 4), MOCK_HOLIDAYS)).toBe(true);
    });

    it('should return true for Sunday', () => {
      // 2026-07-05 is a Sunday
      expect(isNonTradingDay(new Date(2026, 6, 5), MOCK_HOLIDAYS)).toBe(true);
    });

    it('should return true for a closed holiday on a weekday', () => {
      // 2026-07-03 is a Friday (Independence Day observed)
      expect(isNonTradingDay(new Date(2026, 6, 3), MOCK_HOLIDAYS)).toBe(true);
    });

    it('should return false for an early-close day (market is still open)', () => {
      // 2026-11-27 is a Friday (Thanksgiving early close)
      expect(isNonTradingDay(new Date(2026, 10, 27), MOCK_HOLIDAYS)).toBe(false);
    });

    it('should return false for a regular weekday', () => {
      expect(isNonTradingDay(new Date(2026, 6, 6), MOCK_HOLIDAYS)).toBe(false);
    });
  });

  describe('getDisabledHolidayDates', () => {
    it('should return Date objects only for fully closed holidays', () => {
      const disabled = getDisabledHolidayDates(MOCK_HOLIDAYS);
      expect(disabled.length).toBe(2); // July 3 and Dec 25 (not Thanksgiving early close)
    });

    it('should not include early-close dates', () => {
      const disabled = getDisabledHolidayDates(MOCK_HOLIDAYS);
      // Use local date parts to avoid UTC shift issues
      const toLocal = (d: Date) =>
        `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`;
      const dates = disabled.map(toLocal);
      expect(dates).toContain('2026-07-03');
      expect(dates).toContain('2026-12-25');
      expect(dates).not.toContain('2026-11-27');
    });

    it('should return empty array for no holidays', () => {
      expect(getDisabledHolidayDates([])).toEqual([]);
    });
  });

  describe('buildHolidayMap', () => {
    it('should create a map with date keys', () => {
      const map = buildHolidayMap(MOCK_HOLIDAYS);
      expect(map.size).toBe(3);
      expect(map.get('2026-07-03')?.name).toBe('Independence Day');
      expect(map.get('2026-11-27')?.name).toBe('Thanksgiving Day');
      expect(map.get('2026-12-25')?.name).toBe('Christmas Day');
    });

    it('should return empty map for no holidays', () => {
      const map = buildHolidayMap([]);
      expect(map.size).toBe(0);
    });

    it('should skip holidays with null dates', () => {
      const holidays: MarketHolidayEvent[] = [
        { date: null, name: 'Unknown', status: 'Closed', open: null, close: null, exchanges: [] },
        { date: '2026-01-01', name: "New Year's Day", status: 'Closed', open: null, close: null, exchanges: ['NYSE'] },
      ];
      const map = buildHolidayMap(holidays);
      expect(map.size).toBe(1);
    });
  });
});

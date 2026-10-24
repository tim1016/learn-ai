import type { components } from '../../../../src/app/api/broker.types';
import { etMidnightMs } from '../../../../src/app/shared/date/et-midnight';
import { addDays, isWeekend, type IsoDate } from '../../../../src/app/shared/date/iso-date';
import type { BackfillDefaults, StorageSummaryResponse, SymbolCoverageSpan } from '../../../../src/app/shared/data-lake/data-lake.types';
import { NOW_MS } from '../owner-walk-world';
import type { Handler } from './mock-backend';
import { ok } from './mock-backend';

/**
 * What every page that picks an instrument reads first: the vendor's symbol
 * catalog, what the lake holds, the trading calendar and the quick ranges.
 *
 * The walks fix the browser's clock at {@link WALK_NOW_MS} (Monday 2026-09-21,
 * just after the open), so "the last completed session" is Friday 2026-09-18
 * on any day the daily suite runs. The lake holds SPY, QQQ and AAPL through
 * that Friday; the calendar schedules every weekday, with no holiday, from
 * {@link CALENDAR_FIRST_DAY} to {@link CALENDAR_LAST_DAY}.
 */
export const WALK_NOW_MS = NOW_MS;
export const WALK_NOW_DATE: IsoDate = '2026-09-21';
export const LAST_HELD_DAY: IsoDate = '2026-09-18';
export const FIRST_HELD_DAY: IsoDate = '2024-01-02';
export const CALENDAR_FIRST_DAY: IsoDate = '2000-01-03';
export const CALENDAR_LAST_DAY: IsoDate = '2040-12-31';

const HOUR_MS = 60 * 60 * 1000;
const sessionOpenMs = (day: IsoDate): number => etMidnightMs(day) + 9.5 * HOUR_MS;
const sessionCloseMs = (day: IsoDate): number => etMidnightMs(day) + 16 * HOUR_MS;

type SymbolCatalogEntry = components['schemas']['SymbolCatalogEntry'];

/** The listed symbols: the three the lake holds, and one it does not, which a pick would fill first. */
export const VENDOR_SYMBOLS: readonly SymbolCatalogEntry[] = [
  { symbol: 'SPY', name: 'SPDR S&P 500 ETF Trust', exchange: 'ARCX', status: 'active', asset_class: 'us_equity' },
  { symbol: 'QQQ', name: 'Invesco QQQ Trust', exchange: 'XNAS', status: 'active', asset_class: 'us_equity' },
  { symbol: 'AAPL', name: 'Apple Inc.', exchange: 'XNAS', status: 'active', asset_class: 'us_equity' },
  { symbol: 'MSFT', name: 'Microsoft Corporation', exchange: 'XNAS', status: 'active', asset_class: 'us_equity' },
];

/** The instruments the lake holds, with the span each one is held over. */
export const HELD_SYMBOLS: readonly string[] = ['SPY', 'QQQ', 'AAPL'];

const span = (symbol: string, artifactCount: number): SymbolCoverageSpan => ({
  symbol,
  first_trading_date_ms: sessionOpenMs(FIRST_HELD_DAY),
  last_trading_date_ms: sessionOpenMs(LAST_HELD_DAY),
  artifact_count: artifactCount,
});

export const STORAGE_SUMMARY: StorageSummaryResponse = {
  market: 'usa',
  kinds: [],
  symbols: [span('SPY', 680), span('QQQ', 640), span('AAPL', 610)],
};

const BACKFILL_DEFAULTS: BackfillDefaults = {
  market: 'usa',
  lean_image_digest: 'sha256:walk',
  max_trading_range_days: 1830,
  max_symbol_length: 5,
  provider_history_start_ms: sessionOpenMs('2021-09-21'),
};

interface Schedule {
  readonly opens: readonly number[];
  readonly closes: readonly number[];
}

let schedule: Schedule | null = null;

function scheduledSessions(): Schedule {
  if (schedule !== null) return schedule;
  const opens: number[] = [];
  const closes: number[] = [];
  for (let day = CALENDAR_FIRST_DAY; day <= CALENDAR_LAST_DAY; day = addDays(day, 1)) {
    if (isWeekend(day)) continue;
    opens.push(sessionOpenMs(day));
    closes.push(sessionCloseMs(day));
  }
  schedule = { opens, closes };
  return schedule;
}

/** Every weekday from first to last, open 09:30 and close 16:00 ET; the walks' calendar has no holiday. */
function calendarWindow(startMs: number, endMs: number): components['schemas']['TradingCalendarWindow'] {
  const { opens, closes } = scheduledSessions();
  const inside = opens.flatMap((open, index) => (open >= startMs && open <= endMs ? [index] : []));
  return {
    session_open_ms: inside.map((index) => opens[index]),
    session_close_ms: inside.map((index) => closes[index]),
    holidays: [],
  };
}

/** The last `count` sessions that end on the last held day, as Python's range presets resolve them. */
function presetEndingOnLastHeldDay(key: string, label: string, count: number) {
  const days: IsoDate[] = [];
  for (let day = LAST_HELD_DAY; days.length < count; day = addDays(day, -1)) {
    if (!isWeekend(day)) days.unshift(day);
  }
  return {
    key,
    label,
    start_ms_utc: Date.parse(`${days[0]}T00:00:00.000Z`),
    end_ms_utc: Date.parse(`${LAST_HELD_DAY}T23:59:59.999Z`),
    session_count: count,
    estimated_bars_per_timeframe: { '1D': count },
  };
}

export const RANGE_PRESETS = [
  presetEndingOnLastHeldDay('1M', 'Past month', 21),
  presetEndingOnLastHeldDay('3M', 'Past 3 months', 63),
  presetEndingOnLastHeldDay('6M', 'Past 6 months', 126),
  presetEndingOnLastHeldDay('1Y', 'Past year', 252),
];

/** The symbol picker's world and the date pickers': catalog, held span, calendar, provider floor and quick ranges. */
export const lakeHandlers: Handler = ({ method, path, query }) => {
  if (method !== 'GET') return null;
  if (path === '/api/tickers/catalog') return ok(VENDOR_SYMBOLS);
  if (path === '/api/data-lake/storage-summary') return ok(STORAGE_SUMMARY);
  if (path === '/api/data-lake/backfill-defaults') return ok(BACKFILL_DEFAULTS);
  if (path === '/api/calendar/sessions') return ok(calendarWindow(Number(query.get('start_ms')), Number(query.get('end_ms'))));
  if (path === '/api/chart/range-presets') return ok({ presets: RANGE_PRESETS });
  if (path === '/api/jobs') return ok([]);
  return null;
};

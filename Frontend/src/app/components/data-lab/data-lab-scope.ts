import type { QuickRangeKey, QuickRangeOffer } from '../../shared/date-picker';
import type { ChartRangePreset } from '../../services/data-lab-range-presets.service';
import { utcMsToIsoDate } from './data-lab-request-mapper';

/* The scope vocabulary the instrument place and its saved setups share: bar
 * timeframes, the shared quick-range labels for Python's presets, and the one
 * place a calendar date becomes an int64 ms UTC window boundary. Pure. */

export type PolygonTimespan = 'minute' | 'hour' | 'day' | 'week' | 'month';

/**
 * Parse the chart component's timeframe vocabulary ("1m", "5m", "15m",
 * "1h", "4h", "1D", "1W", "1M") into Polygon's (timespan, multiplier)
 * pair. Returns null for unrecognized inputs so callers can ignore
 * vocabulary the dataset endpoint can't honor.
 */
export function parseChartTimeframe(
  timeframe: string,
): { timespan: PolygonTimespan; multiplier: number } | null {
  const match = timeframe.match(/^(\d+)([mhDWM])$/);
  if (!match) return null;
  const multiplier = parseInt(match[1], 10);
  const unit = match[2];
  const timespan: PolygonTimespan | null =
    unit === 'm' ? 'minute' :
    unit === 'h' ? 'hour' :
    unit === 'D' ? 'day' :
    unit === 'W' ? 'week' :
    unit === 'M' ? 'month' :
    null;
  if (!timespan) return null;
  return { timespan, multiplier };
}

/** Bar-timeframe presets: user-facing vocabulary mapped onto (timespan, multiplier). */
export interface BarTimeframeOption {
  value: string;
  label: string;
  timespan: 'minute' | 'hour' | 'day';
  multiplier: number;
}

export const BAR_TIMEFRAMES: readonly BarTimeframeOption[] = [
  { value: '1m', label: '1 min', timespan: 'minute', multiplier: 1 },
  { value: '5m', label: '5 min', timespan: 'minute', multiplier: 5 },
  { value: '15m', label: '15 min', timespan: 'minute', multiplier: 15 },
  { value: '30m', label: '30 min', timespan: 'minute', multiplier: 30 },
  { value: '1h', label: '1 hour', timespan: 'hour', multiplier: 1 },
  { value: '4h', label: '4 hours', timespan: 'hour', multiplier: 4 },
  { value: '1d', label: '1 day', timespan: 'day', multiplier: 1 },
];

/** The bar timeframe a (timespan, multiplier) pair is offered as, or undefined for one nothing offers. */
export function barTimeframeFor(timespan: string, multiplier: number): BarTimeframeOption | undefined {
  return BAR_TIMEFRAMES.find((option) => option.timespan === timespan && option.multiplier === multiplier);
}

/** The bar timeframe named by its value (`5m`, `1d`), or undefined for a value nothing offers. */
export function barTimeframeByValue(value: string): BarTimeframeOption | undefined {
  return BAR_TIMEFRAMES.find((option) => option.value === value);
}

/** Display-only default timeframe per short range preset — a UI default so a
 *  1D window doesn't render as a single daily candle. Longer presets keep the
 *  current timeframe. */
export const PRESET_DEFAULT_TIMEFRAMES: Readonly<Record<string, string>> = {
  '1D': '5m',
  '5D': '30m',
};

/** The shared quick-range label each server preset is shown under: five sessions are a week. */
export const PRESET_LABELS: Readonly<Record<string, QuickRangeKey>> = {
  '1D': '1D',
  '5D': '1W',
  '1M': '1M',
  '3M': '3M',
  '6M': '6M',
  '1Y': '1Y',
  '2Y': '2Y',
  '5Y': '5Y',
};

/** The server's presets as the shared picker's quick ranges: Python's resolved dates under the shared labels. */
export function presetQuickRanges(presets: readonly ChartRangePreset[]): readonly QuickRangeOffer[] {
  return presets.flatMap((preset) => {
    const key = PRESET_LABELS[preset.key];
    return key === undefined
      ? []
      : [{ key, range: { from: utcMsToIsoDate(preset.start_ms_utc), to: utcMsToIsoDate(preset.end_ms_utc) }, unavailable: null }];
  });
}

/** Parse a strict YYYY-MM-DD string to int64 ms UTC at UTC midnight. The
 *  shared `parseYmd` builds LOCAL-midnight Dates, which would make stored
 *  MsUtc window boundaries shift with the viewer's timezone — every
 *  YMD→ms boundary here therefore goes through `Date.UTC`. Pure;
 *  returns null for malformed or impossible calendar dates. */
export function parseYmdMsUtc(s: string): number | null {
  if (!/^\d{4}-\d{2}-\d{2}$/.test(s)) return null;
  const [y, m, d] = s.split('-').map(Number);
  const ms = Date.UTC(y, m - 1, d);
  const check = new Date(ms);
  if (
    check.getUTCFullYear() !== y ||
    check.getUTCMonth() !== m - 1 ||
    check.getUTCDate() !== d
  ) {
    return null;
  }
  return ms;
}

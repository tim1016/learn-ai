export type TimestampDisplayMode = 'local' | 'et' | 'date-et' | 'date-utc';
/** `minute` is `HH:MM` — for a value that lives on a minute boundary (a
 * decision bar's close), where seconds would only add noise. */
export type TimestampGranularity = 'date' | 'time' | 'minute' | 'datetime' | 'chart';

export interface TimestampDisplayOptions {
  mode?: TimestampDisplayMode;
  granularity?: TimestampGranularity;
  localTimeZone?: string;
  fallback?: string;
}

interface WallClockParts {
  year: string;
  month: string;
  day: string;
  hour: string;
  minute: string;
  second: string;
}

const ET_ZONE = 'America/New_York';
const UTC_ZONE = 'UTC';
const DEFAULT_FALLBACK = '—';
const PARTS_OPTIONS: Intl.DateTimeFormatOptions = {
  year: 'numeric',
  month: '2-digit',
  day: '2-digit',
  hour: '2-digit',
  minute: '2-digit',
  second: '2-digit',
  hourCycle: 'h23',
};
// Chart-readout shape (e.g. a candlestick crosshair): no year, short month
// name, seconds precision — distinct enough from PARTS_OPTIONS's zero-padded
// numeric month that it needs its own formatter options.
const CHART_PARTS_OPTIONS: Intl.DateTimeFormatOptions = {
  month: 'short',
  day: '2-digit',
  hour: '2-digit',
  minute: '2-digit',
  second: '2-digit',
  hourCycle: 'h23',
};

function isFiniteMs(value: number | null | undefined): value is number {
  return typeof value === 'number' && Number.isFinite(value);
}

function formatParts(
  ms: number,
  formatOptions: Intl.DateTimeFormatOptions,
  timeZone?: string,
): Partial<Record<Intl.DateTimeFormatPartTypes, string>> {
  const formatter = new Intl.DateTimeFormat(
    'en-US',
    timeZone ? { ...formatOptions, timeZone } : formatOptions,
  );
  const out: Partial<Record<Intl.DateTimeFormatPartTypes, string>> = {};
  for (const part of formatter.formatToParts(new Date(ms))) {
    if (part.type !== 'literal') {
      out[part.type] = part.value;
    }
  }
  return out;
}

function getParts(ms: number, timeZone?: string): WallClockParts {
  return formatParts(ms, PARTS_OPTIONS, timeZone) as WallClockParts;
}

function joinParts(parts: WallClockParts, granularity: TimestampGranularity): string {
  if (granularity === 'date') {
    return `${parts.year}-${parts.month}-${parts.day}`;
  }
  if (granularity === 'time') {
    return `${parts.hour}:${parts.minute}:${parts.second}`;
  }
  if (granularity === 'minute') {
    return `${parts.hour}:${parts.minute}`;
  }
  return `${parts.year}-${parts.month}-${parts.day} ${parts.hour}:${parts.minute}:${parts.second}`;
}

function joinChartParts(parts: Partial<Record<Intl.DateTimeFormatPartTypes, string>>): string {
  return `${parts.month} ${parts.day}, ${parts.hour}:${parts.minute}:${parts.second}`;
}

export function formatTimestampDisplay(
  value: number | null | undefined,
  options: TimestampDisplayOptions = {},
): string {
  const fallback = options.fallback ?? DEFAULT_FALLBACK;
  if (!isFiniteMs(value)) return fallback;

  const mode = options.mode ?? 'local';
  const granularity: TimestampGranularity = mode === 'date-et' || mode === 'date-utc'
    ? 'date'
    : (options.granularity ?? 'datetime');
  const timeZone = mode === 'local'
    ? options.localTimeZone
    : mode === 'date-utc'
      ? UTC_ZONE
      : ET_ZONE;
  const text = granularity === 'chart'
    ? joinChartParts(formatParts(value, CHART_PARTS_OPTIONS, timeZone))
    : joinParts(getParts(value, timeZone), granularity);
  return mode === 'et' ? `${text} ET` : text;
}

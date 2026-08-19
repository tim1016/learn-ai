export type TimestampDisplayMode = 'local' | 'et' | 'date-et' | 'date-utc';
export type TimestampGranularity = 'date' | 'time' | 'datetime' | 'chart';

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

export function formatTimestampIsoInZone(
  value: number | null | undefined,
  timeZone: string,
): string {
  if (!isFiniteMs(value)) return '';
  const instant = new Date(value);
  if (Number.isNaN(instant.getTime())) return '';
  if (timeZone === UTC_ZONE) return instant.toISOString().replace(/\.\d{3}Z$/, 'Z');

  const parts = getParts(value, timeZone);
  const hour = parts.hour === '24' ? '00' : parts.hour;
  const local = `${parts.year}-${parts.month}-${parts.day}T${hour}:${parts.minute}:${parts.second}`;
  const asUtc = Date.UTC(
    Number(parts.year),
    Number(parts.month) - 1,
    Number(parts.day),
    Number(hour),
    Number(parts.minute),
    Number(parts.second),
  );
  const offsetMinutes = Math.round((asUtc - instant.getTime()) / 60000);
  const sign = offsetMinutes >= 0 ? '+' : '-';
  const abs = Math.abs(offsetMinutes);
  const offH = String(Math.floor(abs / 60)).padStart(2, '0');
  const offM = String(abs % 60).padStart(2, '0');
  return `${local}${sign}${offH}:${offM}`;
}

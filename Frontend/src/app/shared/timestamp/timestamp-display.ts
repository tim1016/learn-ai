export type TimestampDisplayMode = 'local' | 'et' | 'date-et';
export type TimestampGranularity = 'date' | 'time' | 'datetime';

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

function isFiniteMs(value: number | null | undefined): value is number {
  return typeof value === 'number' && Number.isFinite(value);
}

function getParts(ms: number, timeZone?: string): WallClockParts {
  const formatter = new Intl.DateTimeFormat(
    'en-US',
    timeZone ? { ...PARTS_OPTIONS, timeZone } : PARTS_OPTIONS,
  );
  const out: Partial<WallClockParts> = {};
  for (const part of formatter.formatToParts(new Date(ms))) {
    if (part.type !== 'literal') {
      out[part.type as keyof WallClockParts] = part.value;
    }
  }
  return out as WallClockParts;
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

export function formatTimestampDisplay(
  value: number | null | undefined,
  options: TimestampDisplayOptions = {},
): string {
  const fallback = options.fallback ?? DEFAULT_FALLBACK;
  if (!isFiniteMs(value)) return fallback;

  const mode = options.mode ?? 'local';
  const granularity: TimestampGranularity = mode === 'date-et'
    ? 'date'
    : (options.granularity ?? 'datetime');
  const timeZone = mode === 'local' ? options.localTimeZone : ET_ZONE;
  const text = joinParts(getParts(value, timeZone), granularity);
  return mode === 'et' && granularity !== 'date' ? `${text} ET` : text;
}

/**
 * Format an ``int64 ms`` epoch into a wall-clock string in a given IANA
 * time zone. The two formats here cover the cockpit's two use cases:
 *
 *   - {@link formatLocalTimestamp} — ``"YYYY-MM-DD HH:MM:SS"`` (incidents
 *     row primary timestamp).
 *   - {@link formatLocalClock} — ``"HH:MM:SS"`` (cockpit shell header
 *     "Local time" pill).
 *
 * The optional ``timeZone`` parameter defaults to the browser's resolved
 * zone. Production callers omit it ("show the operator the wall-clock
 * they look at"); tests pin it ("America/New_York") to assert literal
 * values regardless of the runner's host TZ.
 *
 * Implementation note — uses ``Intl.DateTimeFormat`` with the parts API
 * and ``hourCycle: 'h23'`` so the output is locale-independent and never
 * produces "24" at midnight.
 */

const PARTS_OPTIONS: Intl.DateTimeFormatOptions = {
  year: 'numeric',
  month: '2-digit',
  day: '2-digit',
  hour: '2-digit',
  minute: '2-digit',
  second: '2-digit',
  hourCycle: 'h23',
};

interface WallClockParts {
  year: string;
  month: string;
  day: string;
  hour: string;
  minute: string;
  second: string;
}

function getParts(ms: number, timeZone?: string): WallClockParts {
  const fmt = new Intl.DateTimeFormat(
    'en-US',
    timeZone ? { ...PARTS_OPTIONS, timeZone } : PARTS_OPTIONS,
  );
  const out: Partial<WallClockParts> = {};
  for (const p of fmt.formatToParts(new Date(ms))) {
    if (p.type !== 'literal') {
      (out as Record<string, string>)[p.type] = p.value;
    }
  }
  return out as WallClockParts;
}

export function formatLocalTimestamp(ms: number, timeZone?: string): string {
  const p = getParts(ms, timeZone);
  return `${p.year}-${p.month}-${p.day} ${p.hour}:${p.minute}:${p.second}`;
}

export function formatLocalClock(ms: number, timeZone?: string): string {
  const p = getParts(ms, timeZone);
  return `${p.hour}:${p.minute}:${p.second}`;
}

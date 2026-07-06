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

import { formatTimestampDisplay } from '../shared/timestamp';

export function formatLocalTimestamp(ms: number, timeZone?: string): string {
  return formatTimestampDisplay(ms, {
    mode: 'local',
    granularity: 'datetime',
    localTimeZone: timeZone,
  });
}

export function formatLocalClock(ms: number, timeZone?: string): string {
  return formatTimestampDisplay(ms, {
    mode: 'local',
    granularity: 'time',
    localTimeZone: timeZone,
  });
}

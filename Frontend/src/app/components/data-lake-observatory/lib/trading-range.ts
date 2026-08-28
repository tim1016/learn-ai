/**
 * `MAX_TRADING_RANGE_DAYS` as `PythonDataService/app/data_lake/types.py`
 * declares it: `_MAX_RANGE_YEARS * 366`.
 *
 * Only a fallback. The live cap arrives on `GET /backfill-defaults`, and
 * that is what both forms actually enforce — this is what they use before
 * the data plane has answered, so a window is never waved through just
 * because the defaults read is still in flight. `trading-range.spec.ts`
 * pins it against the backend formula.
 */
export const MAX_TRADING_RANGE_DAYS = 5 * 366;

const ISO_DATE = /^(\d{4})-(\d{2})-(\d{2})$/;
const DAY_MS = 86_400_000;

/**
 * Inclusive calendar-day span of a closed `[start, end]` window — the same
 * formula as the backend's `trading_range_span_days`, which is
 * `(end - start).days + 1`.
 *
 * Calendar days, not trading days: the cap the catalog enforces counts the
 * window, not the sessions inside it. Both endpoints are parsed as UTC
 * midnight so the subtraction can never lose or gain an hour to a DST
 * boundary; these are date-only values and the arithmetic must stay on the
 * calendar.
 *
 * Returns `null` when either endpoint is not a `YYYY-MM-DD` date, which is
 * the shape both `<input type="date">` controls produce and the shape the
 * endpoints' `date` params take.
 */
export function tradingRangeSpanDays(start: string, end: string): number | null {
  const from = parseIsoDateUtc(start);
  const to = parseIsoDateUtc(end);
  if (from === null || to === null) return null;
  return Math.round((to - from) / DAY_MS) + 1;
}

/**
 * Why this window cannot be submitted, or `null` when it can.
 *
 * One home for the check both forms make: the coverage read and the
 * backfill write are capped by the same constant on the same side of the
 * wire, so a window one form accepts and the other refuses would be a bug
 * in this file, not a difference of opinion between the two panels.
 */
export function tradingRangeRejection(
  start: string,
  end: string,
  capDays: number,
): string | null {
  if (start === '' || end === '') return 'Pick a date range.';
  const span = tradingRangeSpanDays(start, end);
  if (span === null) return 'Pick a date range.';
  if (span < 1) return 'The start date is after the end date.';
  if (span > capDays) {
    return `That window is ${span} days; the data plane accepts at most ${capDays}.`;
  }
  return null;
}

function parseIsoDateUtc(value: string): number | null {
  const match = ISO_DATE.exec(value);
  if (match === null) return null;
  const [, year, month, day] = match;
  const ms = Date.UTC(Number(year), Number(month) - 1, Number(day));
  return Number.isNaN(ms) ? null : ms;
}

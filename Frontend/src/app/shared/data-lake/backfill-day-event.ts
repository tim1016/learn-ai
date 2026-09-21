import type { BackfillDayEvent, BackfillFailure } from './data-lake.types';

/**
 * One SSE frame as `JobsService` hands it over: a type plus opaque fields.
 * Exported so the frame's shape is declared once, beside the parser that
 * reads it, rather than re-spelled by each consumer.
 */
export type SseEvent = { readonly type: string } & Readonly<Record<string, unknown>>;

function asString(value: unknown): string | null {
  return typeof value === 'string' ? value : null;
}

function asNumber(value: unknown): number | null {
  return typeof value === 'number' && Number.isFinite(value) ? value : null;
}

function toFailure(raw: unknown): BackfillFailure | null {
  if (typeof raw !== 'object' || raw === null) return null;
  const record = raw as Record<string, unknown>;
  const reason = asString(record['reason']);
  if (reason === null) return null;
  return {
    artifact_kind: asString(record['artifact_kind']) ?? '',
    symbol: asString(record['symbol']),
    trading_date_ms: asNumber(record['trading_date_ms']),
    data_type: asString(record['data_type']),
    reason,
    detail: asString(record['detail']),
    provider_status_code: asNumber(record['provider_status_code']),
    attempt_count: asNumber(record['attempt_count']) ?? 0,
  };
}

/**
 * Parse one `data_lake.backfill_day` frame, or `null` for anything else.
 *
 * The one place the backfill's per-day frame is read. Two surfaces need it
 * and they need it for opposite reasons — the Observatory panel renders
 * every day as a receipt row, while the picker's coverage gate only wants
 * the typed failure that explains why nothing landed — so the parse lives
 * here rather than once per consumer. A second copy is how the two would
 * come to disagree about what a failure is.
 */
export function toBackfillDayEvent(event: SseEvent): BackfillDayEvent | null {
  if (event.type !== 'data_lake.backfill_day') return null;
  const tradingDateMs = asNumber(event['trading_date_ms']);
  const dayIndex = asNumber(event['day_index']);
  const totalDays = asNumber(event['total_days']);
  if (tradingDateMs === null || dayIndex === null || totalDays === null) return null;
  const rawFailures = Array.isArray(event['failures']) ? event['failures'] : [];
  return {
    trading_date_ms: tradingDateMs,
    day_index: dayIndex,
    total_days: totalDays,
    days_remaining: asNumber(event['days_remaining']) ?? Math.max(0, totalDays - dayIndex),
    fetched_count: asNumber(event['fetched_count']) ?? 0,
    reused_count: asNumber(event['reused_count']) ?? 0,
    failures: rawFailures.map(toFailure).filter((f): f is BackfillFailure => f !== null),
  };
}

/**
 * `run_aborted` is the worker's own marker that it stopped early — it names
 * the abort, never its cause. The cause is the sibling failure on the same
 * day (an entitlement, auth or rate-limit refusal), which is the one worth
 * putting in front of an operator.
 */
const ABORT_MARKER_REASON = 'run_aborted';

/**
 * The artifact kind whose failure actually means "this day's bars are not
 * there".
 *
 * Canonical implementation: `_is_bar_affecting_failure` in
 * `PythonDataService/app/data_lake/backfill.py`. A Phase 0 metadata failure
 * (`artifact_kind: "metadata"`, `symbol: null` — a `launcher_unreachable`,
 * say) is day-independent and does **not** stop `ensure_data` from running
 * Pass 1, so it says nothing about whether the day's bars were produced.
 * The data plane learned this the hard way in #1900, where treating any
 * failure as bar-affecting silently zeroed a rollup window. A picker that
 * reported the metadata failure as why a symbol has no bars would be making
 * the same misreading one layer up.
 */
const BAR_ARTIFACT_KIND = 'time_series_bars';

/**
 * The failure that best explains why a run produced nothing, or `null`.
 *
 * Prefers a bar-affecting cause over a day-independent one, and any real
 * cause over the abort marker. Within one day's frame the worker appends
 * Phase 0 metadata failures before Pass 1's bar failures, so position alone
 * would pick the wrong one.
 */
export function rootFailureOf(
  failures: readonly BackfillFailure[],
): BackfillFailure | null {
  const isCause = (failure: BackfillFailure): boolean =>
    failure.reason !== ABORT_MARKER_REASON;
  return (
    failures.find((failure) => failure.artifact_kind === BAR_ARTIFACT_KIND && isCause(failure)) ??
    failures.find(isCause) ??
    failures[0] ??
    null
  );
}

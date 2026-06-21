/**
 * Hand-mirrored types for the incidents panel.
 *
 * Mirrors the Pydantic models on the Python service:
 *   - app/services/live_log_failures.py :: IncidentCategory, IncidentRow
 *   - app/schemas/live_runs.py :: IncidentRecord
 *
 * Per the #565 frontend-contracts decision, the `IncidentCategory` enum
 * is mirrored hand-typed here rather than regenerated through the broker
 * OpenAPI types so a future regen does not churn unrelated types.
 *
 * `ts_ms` is documented as ordering / cursor-only under the existing log
 * parser contract (it is `raw_ts` parsed as if UTC, which is host-local
 * for engines whose host TZ ≠ UTC). Tables and drawer headers render
 * `raw_ts` for absolute display until the engine emits canonical UTC ms
 * at source.
 */

export const INCIDENT_CATEGORIES = [
  'broker_disconnect',
  'broker_reconnect_failed',
  'engine_fatal',
  'portfolio_init_fail',
  'reconcile_missing',
  'lost_fill',
  'outside_mutation',
  'cold_start_divergence',
  'operator_halt',
  'subscription_stale',
  'unknown',
] as const;

export type IncidentCategory = (typeof INCIDENT_CATEGORIES)[number];

export type IncidentLevel = 'WARNING' | 'ERROR' | 'CRITICAL';

/** One parsed WARNING / ERROR / CRITICAL block from live.log, tagged with
 * a backend-classified incident category. See the file-level docstring
 * for `raw_ts` / `ts_ms` semantics. */
export interface IncidentRow {
  ts_ms: number;
  raw_ts: string;
  level: IncidentLevel;
  logger: string;
  message: string;
  traceback: string | null;
  incident_category: IncidentCategory;
}

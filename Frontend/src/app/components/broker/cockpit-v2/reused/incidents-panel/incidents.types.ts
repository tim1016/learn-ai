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
 * `ts_ms` is canonical `int64` ms since Unix epoch UTC. The engine
 * logger's `_StepFormatter` pins `time.gmtime`, so live.log timestamps
 * are wall-clock UTC at source and the parser produces canonical ms.
 * The cockpit renders `ts_ms` in the viewer's TZ for the primary
 * timestamp and keeps `raw_ts` (verbatim UTC string from the log) beside
 * it for cross-referencing live.log.
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
  'data_farm_degraded',
  'broker_event_log_write_failed',
  'foreign_fill_dropped',
  'shutdown_flatten_failed',
  'control_plane_lease_lost',
  'sidecar_schema_drift',
  'unknown',
] as const;

export type IncidentCategory = (typeof INCIDENT_CATEGORIES)[number];

export type IncidentLevel = 'WARNING' | 'ERROR' | 'CRITICAL';

/**
 * Source dimension paired with `IncidentCategory`. Whereas the category
 * answers *what* failed, the source answers *whose action recovers it* —
 * so the cockpit can badge rows and let the operator filter by side.
 *
 * Mirrors the backend's `IncidentSource` enum (codex 2026-06-24 D2).
 */
export const INCIDENT_SOURCES = ['broker', 'app', 'infra', 'operator', 'unknown'] as const;
export type IncidentSource = (typeof INCIDENT_SOURCES)[number];

/** One parsed WARNING / ERROR / CRITICAL block from live.log, tagged with
 * a backend-classified incident category. See the file-level docstring
 * for `raw_ts` / `ts_ms` semantics.
 *
 * `incident_source` and `dynamic_facts` are typed as optional because the
 * rollout sequence (D8) lets the backend ship them before this frontend
 * deploys; a row without `incident_source` renders the UNKNOWN badge
 * rather than blowing up. Once the rollout window closes both can be
 * tightened to required. */
export interface IncidentRow {
  ts_ms: number;
  raw_ts: string;
  level: IncidentLevel;
  logger: string;
  message: string;
  traceback: string | null;
  incident_category: IncidentCategory;
  incident_source?: IncidentSource;
  dynamic_facts?: Record<string, string | number>;
}

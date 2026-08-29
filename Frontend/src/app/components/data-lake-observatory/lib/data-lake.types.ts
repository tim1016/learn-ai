/**
 * Wire shapes for the flag-gated data-lake surface (`PythonDataService`
 * `app/routers/data_lake.py`, `app/data_lake/types.py`).
 *
 * Hand-written rather than generated: `main.py` only mounts the data-lake
 * router when `DATA_LAKE_ENABLED` is on, so the routes are absent from the
 * committed OpenAPI contract these types would otherwise be generated from
 * (`contracts/openapi/python-data-service.openapi.json`). Field names are
 * snake_case because the data plane is reached directly, not through the
 * .NET proxy.
 *
 * Every temporal field is `int64 ms UTC` (`.claude/rules/temporal-rigor.md`).
 * `*_trading_date_ms` values are date-anchored at the 09:30 ET session open
 * and must be rendered in `date-et` mode, never viewer-local.
 */

/** Catalog `Status` column plus the coverage endpoint's synthesized `missing`. */
export type ArtifactStatus = 'fetching' | 'complete' | 'stale' | 'failed';
export type CoverageStatus = ArtifactStatus | 'missing';

export type DataLakeDataType = 'trade' | 'quote';
export type PriceAdjustmentMode = 'raw' | 'polygon_split_adjusted' | 'lean_adjusted';

export interface CoverageDay {
  /** Session open (09:30 ET) as int64 ms UTC. Date-anchored. */
  readonly trading_date_ms: number;
  readonly status: CoverageStatus;
  readonly artifact_id: number | null;
}

export interface CoverageResponse {
  readonly market: string;
  readonly symbol: string;
  readonly data_type: string;
  readonly resolution: string;
  readonly provider: string;
  readonly price_adjustment_mode: string;
  readonly days: readonly CoverageDay[];
}

export interface ArtifactDetail {
  readonly id: number;
  readonly artifact_kind: string;
  readonly market: string | null;
  readonly symbol: string | null;
  readonly trading_date_ms: number | null;
  readonly resolution: string | null;
  readonly data_type: string | null;
  readonly provider: string;
  readonly provider_params: Readonly<Record<string, unknown>>;
  readonly price_adjustment_mode: string | null;
  readonly data_contract_hash: string;
  /** Null until the row reaches `complete` — never an empty string. */
  readonly content_hash: string | null;
  readonly file_path: string;
  readonly file_size_bytes: number | null;
  readonly status: ArtifactStatus;
  readonly row_count: number | null;
  readonly first_bar_start_ms: number | null;
  readonly last_bar_start_ms: number | null;
  readonly fetched_at_ms: number;
  readonly completed_at_ms: number | null;
  readonly attempt_count: number;
  readonly last_error: string | null;
  readonly error_message: string | null;
}

export interface StorageKindTotal {
  readonly artifact_kind: string;
  readonly resolution: string | null;
  readonly artifact_count: number;
  readonly total_bytes: number;
}

export interface SymbolCoverageSpan {
  readonly symbol: string;
  readonly first_trading_date_ms: number | null;
  readonly last_trading_date_ms: number | null;
  readonly artifact_count: number;
}

export interface StorageSummaryResponse {
  readonly market: string;
  readonly kinds: readonly StorageKindTotal[];
  readonly symbols: readonly SymbolCoverageSpan[];
}

export interface BackfillDefaults {
  readonly market: string;
  /** Null when the data plane has no pinned LEAN image — backfill is then unavailable. */
  readonly lean_image_digest: string | null;
  readonly max_trading_range_days: number;
  readonly max_symbol_length: number;
}

/**
 * `ArtifactFailure.reason` — the typed vocabulary `app/data_lake/types.py`
 * pins. Kept open-ended (`| string`) on purpose: a reason this build does not
 * know still reaches the operator through the receipt-label pipe rather than
 * being swallowed.
 */
export type ArtifactFailureReason =
  | 'provider_auth_error'
  | 'provider_entitlement_error'
  | 'provider_rate_limited'
  | 'provider_api_error'
  | 'provider_no_data'
  | 'unknown_symbol'
  | 'validation_failed'
  | 'io_error'
  | 'lease_timeout'
  | 'fetch_timeout'
  | 'unsupported_resolution'
  | 'unsupported_artifact_kind'
  | 'corp_action_revision_mismatch'
  | 'data_contract_mismatch'
  | 'internal_error'
  | 'session_not_produced'
  | 'run_aborted';

/** One typed failure as the backfill job puts it on the wire. */
export interface BackfillFailure {
  readonly artifact_kind: string;
  readonly symbol: string | null;
  /** Date-anchored session open, or null when the failure is not day-scoped. */
  readonly trading_date_ms: number | null;
  readonly data_type: string | null;
  readonly reason: ArtifactFailureReason | string;
  readonly detail: string | null;
  readonly provider_status_code: number | null;
  readonly attempt_count: number;
}

/** Payload of the `data_lake.backfill_day` SSE domain event. */
export interface BackfillDayEvent {
  readonly trading_date_ms: number;
  readonly day_index: number;
  readonly total_days: number;
  readonly days_remaining: number;
  readonly fetched_count: number;
  readonly reused_count: number;
  readonly failures: readonly BackfillFailure[];
}

/**
 * The `DataRunSpec` body the backfill job accepts.
 *
 * `start_trading_date` / `end_trading_date` are `YYYY-MM-DD` because that is
 * what the endpoint's `date`-typed fields take; they are the one place a
 * caller does not get to choose the representation. The range-scoped
 * `include_factor_files` / `include_map_files` / `include_daily_trade`
 * switches are deliberately absent: `run_backfill` overrides all three for
 * its per-day sub-calls, so offering them here would be a lie.
 */
export interface DataRunSpec {
  readonly request_id: string;
  readonly run_type: 'python_lab' | 'lean_lab';
  readonly market: string;
  readonly symbols: readonly string[];
  readonly start_trading_date: string;
  readonly end_trading_date: string;
  readonly data_types: readonly DataLakeDataType[];
  readonly lean_image_digest: string;
  readonly force_refresh: boolean;
  /**
   * Which adjustment mode the fetch writes. Omitted means `raw`, matching
   * the data plane's own default. `lean_adjusted` is deliberately absent:
   * it would be derived from raw bars plus factor files and no producer
   * exists, so the panel refuses it rather than sending a mode the
   * pipeline cannot fulfil.
   */
  readonly price_adjustment_mode?: 'raw' | 'polygon_split_adjusted';
}

/**
 * Every way a data-lake read can fail to produce a value.
 *
 * `not_enabled` is a first-class outcome, not an error: the lake is dark in
 * production until the enablement slice flips `DATA_LAKE_ENABLED`, and a
 * dark router answers a bare 404 on every route. `rejected` carries the
 * endpoint's own typed `{reason, message}` body verbatim so the reason
 * renders through the receipt-label pipe instead of being re-worded here —
 * that covers both the 422 validators and the `artifact_not_found` 404.
 *
 * Named separately from `DataLakeRead` because classification only ever
 * produces a failure; a function that cannot return `ok` should not be
 * typed as if it might.
 */
export type DataLakeFailure =
  | { readonly kind: 'not_enabled' }
  | { readonly kind: 'rejected'; readonly reason: string; readonly message: string }
  | { readonly kind: 'unavailable'; readonly message: string };

/** Every data-lake read resolves to exactly one of these. */
export type DataLakeRead<T> = { readonly kind: 'ok'; readonly value: T } | DataLakeFailure;

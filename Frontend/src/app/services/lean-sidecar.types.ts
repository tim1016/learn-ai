/**
 * TypeScript shapes for the LEAN Sidecar Lab data-plane endpoints.
 *
 * These mirror the Pydantic models in
 * `PythonDataService/app/routers/lean_sidecar.py` and
 * `PythonDataService/app/lean_sidecar/normalized_parser.py`. Keep them
 * in lockstep with the Python side — the Phase 2a/3a contract says
 * the wire format is the source of truth.
 *
 * Per `.claude/rules/numerical-rigor.md`, every timestamp on the wire
 * is `int64 ms UTC`. TypeScript can't represent int64 exactly past
 * 2^53, but for our 2026-era timestamps `number` is faithful (we're
 * about 30,000 years away from precision loss).
 */

/** Caller-supplied run request. Optional ``algorithm_source`` since Phase 4c. */
export interface TrustedRunRequest {
  /** Strict slug `^[a-z0-9][a-z0-9_-]{2,63}$`. */
  run_id: string;
  /** Equity ticker, `^[A-Za-z0-9.\-]{1,16}$`. Defaults to SPY on the server. */
  symbol?: string;
  /** Inclusive window start as int64 ms UTC. */
  start_ms_utc: number;
  /** Inclusive window end as int64 ms UTC; must be strictly > start. */
  end_ms_utc: number;
  /** Starting capital in USD; server cap is $1,000..$10,000,000. */
  starting_cash?: number;
  /**
   * Optional QCAlgorithm Python source. When omitted, the bundled
   * trusted buy_and_hold sample runs on the server. Must define a
   * class named ``MyAlgorithm`` and stay under the server's 256 KiB
   * cap. Runs inside the Phase 1c hardened sandbox (read-only root,
   * non-root user, no caps, no network, workspace-only mount).
   */
  algorithm_source?: string | null;
  /**
   * Phase 5b — which bundled trusted sample the server stages when
   * ``algorithm_source`` is omitted. ``trusted_default`` (back-compat
   * default) runs the LEAN-default-brokerage sample; ``reconciliation``
   * runs the IBKR-brokerage-pinned sample that the Phase 5a fee
   * reconciler returns a clean report for. Ignored when
   * ``algorithm_source`` is provided (operator-pasted source pins its
   * own brokerage via SetBrokerageModel).
   */
  template?: "trusted_default" | "reconciliation";
}

/**
 * Classified LEAN error categories — the launcher buckets every
 * `ERROR::` line from LEAN's `log.txt` into one of these.
 * Stable strings the UI can branch on without parsing free text.
 */
export interface LeanErrorBuckets {
  analysis_failed: string[];
  failed_data_requests: string[];
  runtime_error: string[];
  other: string[];
}

export interface TrustedRunResponse {
  run_id: string;
  /** True iff exit_code==0 AND no LEAN errors AND not timed out. */
  is_clean: boolean;
  exit_code: number;
  duration_ms: number;
  timed_out: boolean;
  lean_errors: LeanErrorBuckets;
  /** Trailing slice of the LEAN container's combined stdout+stderr. */
  log_tail: string;
  /** Server-side absolute paths for human/operator inspection. */
  manifest_path: string;
  workspace_root: string;
  observations_path: string;
  lean_log_path: string;
  /** Phase 3a parser output — present iff LEAN produced parseable artifacts. */
  normalized_path: string | null;
  normalized_parser_version: string | null;
  total_order_events: number | null;
  total_equity_points: number | null;
}

/**
 * One point on the equity curve returned by `/runs/{id}/normalized`.
 * LEAN samples on bar boundaries; sub-second resolution is not meaningful.
 */
export interface NormalizedEquityPoint {
  ms_utc: number;
  value: number;
  open: number;
  high: number;
  low: number;
}

/**
 * One order event from LEAN. LEAN typically emits ``submitted`` then
 * ``filled`` for each market order; fees + fill price live on the
 * filled event.
 */
export interface NormalizedOrderEvent {
  order_event_id: number;
  order_id: number;
  algorithm_id: string;
  symbol: string;
  symbol_value: string;
  ms_utc: number;
  status: string;
  direction: string;
  quantity: number;
  fill_price: number;
  fill_price_currency: string;
  fill_quantity: number;
  is_assignment: boolean;
  order_fee_amount: number | null;
  order_fee_currency: string | null;
  message: string | null;
}

/**
 * Full parsed result the `/runs/{id}/normalized` endpoint serves.
 * Statistics are strings — LEAN's stats are version- and definition-
 * sensitive (Sharpe annualization, sample vs population stdev,
 * benchmark selection); the UI shouldn't parse them into floats
 * without pinning a convention.
 */
export interface NormalizedResult {
  parser_version: string;
  algorithm_id: string;
  statistics: Record<string, string>;
  runtime_statistics: Record<string, string>;
  equity_curve: NormalizedEquityPoint[];
  order_events: NormalizedOrderEvent[];
  total_order_events: number;
  total_equity_points: number;
  first_equity_ms_utc: number | null;
  last_equity_ms_utc: number | null;
}

/**
 * Shape of the launcher/service rejection envelope. The router mirrors
 * the launcher's `{detail: {reason, message}}` for 4xx responses so a
 * caller can branch on `reason` without parsing free text.
 */
export interface LeanSidecarErrorEnvelope {
  reason: string;
  message: string;
}

/**
 * Phase 4e — the minimal slice of the manifest the UI uses to
 * rehydrate the form on sidebar click. The full server-side manifest
 * has many more fields (see RunManifest in
 * PythonDataService/app/lean_sidecar/manifest.py); the UI only needs
 * the inputs that were on the form when this run was submitted, so
 * the rest is intentionally not typed here. Read with optional
 * fallbacks — older manifests may not have every nested field.
 */
export interface RunManifest {
  run_id?: string;
  parameters?: {
    symbol?: string;
    starting_cash?: string | number;
    [k: string]: unknown;
  };
  requested_window_ms?: {
    start_ms: number;
    end_ms: number;
  };
  algorithm_source_sha256?: string;
  [k: string]: unknown;
}

/** One row from GET /api/lean-sidecar/runs — Phase 4d index. */
export interface RunSummary {
  run_id: string;
  symbol: string | null;
  requested_start_ms_utc: number | null;
  requested_end_ms_utc: number | null;
  started_at_ms: number | null;
  finished_at_ms: number | null;
  exit_code: number | null;
  algorithm_source_kind: "trusted_sample" | "user_provided" | "unknown";
  /**
   * ``exit_code == 0`` — a fast at-a-glance status. NOT a substitute
   * for ``is_clean`` (which also requires zero classified LEAN
   * errors); the sidebar uses this only to color rows.
   */
  exit_clean: boolean | null;
  /**
   * The true cleanliness signal: extracted from the manifest's
   * ``is_clean=<bool>`` note (Phase 2a+). ``null`` for legacy
   * manifests that predate the note. ``loadRun`` uses this (not
   * ``exit_clean``) when synthesizing a rehydrated TrustedRunResponse
   * so a run that exited 0 with classified LEAN errors does NOT
   * paint as a green "Clean run" badge.
   */
  is_clean: boolean | null;
}

export interface RunIndexResponse {
  runs: RunSummary[];
  cap: number;
  truncated: boolean;
}

/**
 * Phase 5a — one row in the fee-reconciliation report. Money values
 * arrive as strings (preserves exact cents through JSON serialization;
 * matches the Decimal hygiene called out in the ADR's Phase 5a
 * section). Categories mirror ``FeeDivergenceCategory`` in
 * ``PythonDataService/app/lean_sidecar/reconciler.py``.
 */
export interface FeeDivergence {
  order_event_id: number;
  order_id: number;
  symbol: string;
  ms_utc: number;
  fill_quantity: number;
  fill_price: string;
  recorded_fee: string | null;
  expected_ibkr_fee: string;
  delta: string | null;
  category: "commission_drift" | "no_recorded_fee" | "fractional_quantity";
  /**
   * Populated only when ``category == "fractional_quantity"``. Carries
   * the original float fill quantity LEAN emitted (e.g., 100.5) so the
   * operator can see the value before integer rounding would have been
   * applied. The integer ``fill_quantity`` field above is the truncated
   * value the IBKR model would have charged against if the reconciler
   * had not bailed.
   */
  fill_quantity_raw?: number | null;
}

/**
 * Phase 5a — full report returned by
 * ``POST /api/lean-sidecar/runs/{id}/reconcile``.
 *
 * ``run_id`` is the workspace slug (path parameter), ``algorithm_id`` is
 * LEAN's algorithm-type-name — they are distinct because the slug is
 * UI-generated while the algorithm-id defaults to ``MyAlgorithm``.
 */
export interface RunReconciliationReport {
  run_id: string;
  algorithm_id: string;
  /**
   * Parser-version pin recorded with the ``result.json`` the report was
   * computed from. Two reports are directly comparable only when their
   * ``normalized_parser_version`` matches; a bump means the upstream
   * normalization may have changed.
   */
  normalized_parser_version: string;
  total_fill_events: number;
  matched_count: number;
  divergent_count: number;
  commission_atol: string;
  total_recorded_fees: string;
  total_expected_ibkr_fees: string;
  divergences: FeeDivergence[];
}

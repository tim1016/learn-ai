/**
 * TypeScript shapes for the LEAN Sidecar Lab data-plane endpoints.
 *
 * Mirrors the Pydantic models in
 * ``PythonDataService/app/routers/lean_sidecar.py``. Keep them in
 * lockstep with the Python side — the Phase 2a/3a contract says the
 * wire format is the source of truth.
 *
 * Per ``.claude/rules/numerical-rigor.md``, every timestamp on the wire
 * is ``int64 ms UTC``. TypeScript can't represent int64 exactly past
 * 2^53, but for our 2026-era timestamps ``number`` is faithful.
 *
 * PR B.5 (2026-05-19) — surface narrowed to what the unified Engine
 * Lab needs: ``startTrustedRun`` request/response and the launcher's
 * error envelope. Inspection / reconciliation / manifest / log-tail
 * types were removed alongside the ``/lean-lab`` retirement; check git
 * history if a future feature needs to revive any of them.
 */

import type { DataPolicy } from "../models/data-policy";

/** Caller-supplied run request. Optional ``algorithm_source`` since Phase 4c. */
export interface TrustedRunRequest {
  /** Strict slug ``^[a-z0-9][a-z0-9_-]{2,63}$``. */
  run_id: string;
  /**
   * Equity ticker, ``^[A-Za-z0-9.\-]{1,16}$``. Defaults to SPY on the server.
   *
   * @deprecated PR B (2026-05-19) — moved into ``data_policy.symbol``.
   * The router still accepts top-level ``symbol`` for one deprecation cycle.
   * New callers send ``data_policy`` instead.
   */
  symbol?: string;
  /**
   * P2.5 contract — 09:30 ET (NYSE session-open) of the first trading
   * day, expressed as int64 ms UTC. The conversion goes through the
   * NY zone (DST-aware); a fixed-offset converter produces silent
   * 1-hour bugs on either side of 2026-03-08 / 2026-11-01.
   */
  start_ms_utc: number;
  /**
   * P2.5 contract — 09:30 ET of ``next_trading_day(end_date)``,
   * expressed as int64 ms UTC. Half-open ``[start_ms_utc, end_ms_utc)``,
   * so ``end_ms_utc`` is the EXCLUSIVE end. Must be strictly > start.
   */
  end_ms_utc: number;
  /** Starting capital in USD; server cap is $1,000..$10,000,000. */
  starting_cash?: number;
  /**
   * Optional QCAlgorithm Python source. When omitted, the bundled
   * trusted ``buy_and_hold`` sample runs on the server. Must define a
   * class named ``MyAlgorithm`` and stay under the server's 256 KiB
   * cap.
   */
  algorithm_source?: string | null;
  /**
   * Which bundled trusted sample the server stages when
   * ``algorithm_source`` is omitted. Ignored when ``algorithm_source``
   * is provided (operator-pasted source pins its own brokerage via
   * ``SetBrokerageModel``).
   */
  template?: "trusted_default" | "reconciliation" | "ema_crossover" | "deployment_validation";
  /**
   * PR B (2026-05-19) — canonical DataPolicy block. When provided, the
   * legacy top-level ``symbol`` field must be omitted (the router rejects
   * mixed shapes with HTTP 422).
   */
  data_policy?: DataPolicy;
}

/**
 * Classified LEAN error categories — the launcher buckets every
 * ``ERROR::`` line from LEAN's ``log.txt`` into one of these.
 *
 * ``benchmark_unavailable`` is the one non-gating bucket: it absorbs
 * LEAN's default-benchmark SPY-zip miss + the post-strategy
 * ``ReadEquityCurve`` / ``Sequence contains no elements`` cascade so a
 * run that produced trades and a STATISTICS:: block is reported as
 * clean. See ``PythonDataService/app/lean_sidecar/result_classifier.py``
 * for the matching rules.
 */
export interface LeanErrorBuckets {
  analysis_failed: string[];
  failed_data_requests: string[];
  runtime_error: string[];
  benchmark_unavailable: string[];
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
  /**
   * PR #291 — the ``StrategyExecution.Id`` row written to Postgres at
   * the tail of ``run_trusted_sample()``. ``null`` when persistence is
   * disabled or the run failed before normalization.
   */
  strategy_execution_id: number | null;
}

/**
 * Shape of the launcher / service rejection envelope. The router
 * mirrors the launcher's ``{detail: {reason, message}}`` for 4xx
 * responses so a caller can branch on ``reason`` without parsing free
 * text.
 */
export interface LeanSidecarErrorEnvelope {
  reason: string;
  message: string;
}

export type LeanLauncherCheckStatus = "pass" | "fail" | "warn";

export interface LeanLauncherDiagnosticCheck {
  name: string;
  label: string;
  status: LeanLauncherCheckStatus;
  detail: string;
  fix: string | null;
}

export interface LeanLauncherDiagnosticReport {
  overall_status: LeanLauncherCheckStatus;
  checks: LeanLauncherDiagnosticCheck[];
  fetched_at_ms: number;
}

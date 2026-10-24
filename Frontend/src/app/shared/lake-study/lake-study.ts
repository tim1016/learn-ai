/**
 * What a lake-sourced research study says about its data, for display (#3521).
 *
 * Every study that computes from the data lake (Feature, Signal, Reliability,
 * Cross-sectional) returns the same `data_source` block and, when the lake could
 * not supply its inputs, the same refusal payload
 * (`PythonDataService/app/research/lake_study.py`). This file is the one place
 * the browser reads them: the wire types as the server sends them, the mapping
 * to camelCase, and the projection onto the shared data-source line's
 * `DataSourceProvenance`. A study's host maps with these and renders with
 * `LakeStudySourceComponent`; it declares no coverage or refusal types of its own.
 *
 * Nothing here derives a fact. A time the server did not record is `null`.
 */
import type { Session } from '../ticker-range-picker/ticker-range-picker.types';
import type { DataSourceProvenance } from '../workspace/data-source-provenance';

// ---------------------------------------------------------------------------
// The wire (snake_case, as `data_source_block` and `StudyInputRefusal.as_dict` write it)
// ---------------------------------------------------------------------------

/** `RefusalCode` in `app/research/study_inputs.py`. */
export type LakeRefusalCode =
  | 'resolution_unsupported'
  | 'symbol_not_addressable'
  | 'no_sessions_in_window'
  | 'window_not_complete'
  | 'data_unavailable_from_provider'
  | 'data_corrupt'
  | 'captured_before_final'
  | 'data_building'
  | 'data_missing'
  | 'lake_unavailable';

/**
 * The session a lake study reads: `regular` is the scheduled session, `extended` every bar the provider served.
 * The server's name (`SessionPolicy` in `app/research/study_inputs.py`); a request spells the same two choices `rth` and `extended`.
 */
export type LakeSessionPolicy = 'extended' | 'regular';

/** What a request's `session` asks the lake for (`SESSION_POLICY_FOR_REQUEST` in `app/research/lake_study.py`). */
export const SESSION_POLICY_FOR_REQUEST: Readonly<Record<Session, LakeSessionPolicy>> = {
  rth: 'regular',
  extended: 'extended',
};

/** A session in the reader's words, for a summary line or a refusal. */
export function sessionPolicyWords(policy: LakeSessionPolicy): string {
  return policy === 'regular' ? 'regular hours' : 'extended hours';
}

/** What admission concluded about a session it could not make ready. */
export type LakeSessionOutcome =
  | 'missing'
  | 'building'
  | 'corrupt'
  | 'captured_before_final'
  | 'unavailable_from_provider';

export interface LakeCoverageGroupRaw {
  expected_sessions: number;
  reused: number;
  added: number;
  missing: number;
  failed: number;
  building: number;
  corrupt: number;
  captured_before_final: number;
  unavailable_from_provider: number;
}

export interface LakeCoverageRaw {
  window: LakeCoverageGroupRaw;
  warmup: LakeCoverageGroupRaw;
  artifacts: { fetched: number; reused: number; refreshed: number };
}

/** The `data_source` block of a lake study's result or refusal. A study adds its own identity fields beside these. */
export interface LakeDataSourceRaw {
  kind: 'lake';
  provider: string;
  symbol: string;
  price_adjustment_mode: string;
  session_policy: LakeSessionPolicy;
  /** The requested bounds, `int64 ms UTC` at ET midnight. */
  window_start_ms: number;
  window_end_ms: number;
  /** The first and last session admitted (ET midnight); `null` until admission succeeds. */
  effective_window: { start_ms: number; end_ms: number } | null;
  warmup_sessions: number;
  bar_minutes: number;
  aggregation: string;
  identity_digest: string;
  calculation_version: string;
  /** sha256 of every byte read; `null` when nothing was admitted. */
  snapshot_digest: string | null;
  coverage: LakeCoverageRaw | null;
  bars_used: number | null;
  cache: { hit: boolean; cached_at_ms: number | null };
}

export interface LakeSessionIssueRaw {
  group: 'window' | 'warmup';
  /** The session's canonical open, `int64 ms UTC`. */
  trading_date_ms: number;
  outcome: LakeSessionOutcome;
  reason: string;
  /** The capture's own reason code for the session (`ArtifactFailureReason`), when it reported one. */
  failure_reason: string | null;
}

export interface LakeRefusalRaw {
  code: LakeRefusalCode;
  reason: string;
  sessions: LakeSessionIssueRaw[];
  sessions_total: number;
  coverage: LakeCoverageRaw | null;
}

/** What a refused study parks as its job result. */
export interface LakeRefusedResultRaw {
  success: false;
  status: 'refused';
  refusal: LakeRefusalRaw;
  data_source: LakeDataSourceRaw | null;
}

// ---------------------------------------------------------------------------
// The model
// ---------------------------------------------------------------------------

/** Distinct-session counts for the window or its warmup. */
export interface LakeCoverageGroup {
  expectedSessions: number;
  /** Ready before this run began. */
  reused: number;
  /** Made ready by this run. */
  added: number;
  /** Not held, and not captured. */
  missing: number;
  /** `building + corrupt + capturedBeforeFinal + unavailableFromProvider`. */
  failed: number;
  building: number;
  corrupt: number;
  /** Held, but captured before the session's data stopped forming; its tail is missing. */
  capturedBeforeFinal: number;
  unavailableFromProvider: number;
}

export interface LakeCoverage {
  window: LakeCoverageGroup;
  warmup: LakeCoverageGroup;
  artifacts: { fetched: number; reused: number; refreshed: number };
}

export interface LakeStudyDataSource {
  provider: string;
  symbol: string;
  priceAdjustmentMode: string;
  /** `extended` is every bar the provider served for the day; `regular` is the scheduled session. */
  sessionPolicy: LakeSessionPolicy;
  windowStartMs: number;
  windowEndMs: number;
  effectiveWindow: { startMs: number; endMs: number } | null;
  warmupSessions: number;
  barMinutes: number;
  aggregation: string;
  identityDigest: string;
  calculationVersion: string;
  snapshotDigest: string | null;
  coverage: LakeCoverage | null;
  barsUsed: number | null;
  cache: { hit: boolean; cachedAtMs: number | null };
}

export interface LakeSessionIssue {
  group: 'window' | 'warmup';
  tradingDateMs: number;
  outcome: LakeSessionOutcome;
  reason: string;
  failureReason: string | null;
}

/** Why the lake could not supply a study's inputs, and what admission did before it stopped. */
export interface LakeStudyRefusal {
  code: LakeRefusalCode;
  reason: string;
  /** The sessions that could not be made ready; the first of `sessionsTotal`. */
  sessions: readonly LakeSessionIssue[];
  sessionsTotal: number;
  coverage: LakeCoverage | null;
}

/** A refused study: the refusal, and the source it was refused against (`null` when nothing was read). */
export interface LakeStudyRefused {
  refusal: LakeStudyRefusal;
  dataSource: LakeStudyDataSource | null;
}

// ---------------------------------------------------------------------------
// Mapping
// ---------------------------------------------------------------------------

function mapGroup(raw: LakeCoverageGroupRaw): LakeCoverageGroup {
  return {
    expectedSessions: raw.expected_sessions,
    reused: raw.reused,
    added: raw.added,
    missing: raw.missing,
    failed: raw.failed,
    building: raw.building,
    corrupt: raw.corrupt,
    capturedBeforeFinal: raw.captured_before_final,
    unavailableFromProvider: raw.unavailable_from_provider,
  };
}

function mapCoverage(raw: LakeCoverageRaw | null): LakeCoverage | null {
  return raw === null ? null : { window: mapGroup(raw.window), warmup: mapGroup(raw.warmup), artifacts: raw.artifacts };
}

export function mapLakeStudyDataSource(raw: LakeDataSourceRaw): LakeStudyDataSource {
  return {
    provider: raw.provider,
    symbol: raw.symbol,
    priceAdjustmentMode: raw.price_adjustment_mode,
    sessionPolicy: raw.session_policy,
    windowStartMs: raw.window_start_ms,
    windowEndMs: raw.window_end_ms,
    effectiveWindow: raw.effective_window === null ? null : { startMs: raw.effective_window.start_ms, endMs: raw.effective_window.end_ms },
    warmupSessions: raw.warmup_sessions,
    barMinutes: raw.bar_minutes,
    aggregation: raw.aggregation,
    identityDigest: raw.identity_digest,
    calculationVersion: raw.calculation_version,
    snapshotDigest: raw.snapshot_digest,
    coverage: mapCoverage(raw.coverage),
    barsUsed: raw.bars_used,
    cache: { hit: raw.cache.hit, cachedAtMs: raw.cache.cached_at_ms },
  };
}

export function mapLakeStudyRefusal(raw: LakeRefusalRaw): LakeStudyRefusal {
  return {
    code: raw.code,
    reason: raw.reason,
    sessions: raw.sessions.map((s) => ({
      group: s.group,
      tradingDateMs: s.trading_date_ms,
      outcome: s.outcome,
      reason: s.reason,
      failureReason: s.failure_reason,
    })),
    sessionsTotal: raw.sessions_total,
    coverage: mapCoverage(raw.coverage),
  };
}

/**
 * The refusal a failed job parked as its result, or `null` when the payload is not one (the job failed for another
 * reason and parked nothing, or something else). A host calls this on whatever `fetchResult` returns for a failed job.
 */
export function readLakeStudyRefusal(payload: unknown): LakeStudyRefused | null {
  if (typeof payload !== 'object' || payload === null) return null;
  // A wire payload of unknown shape: narrowed below by checking the two fields that make it a refusal.
  const candidate = payload as Partial<LakeRefusedResultRaw>;
  if (candidate.status !== 'refused' || typeof candidate.refusal !== 'object' || candidate.refusal === null) return null;
  return {
    refusal: mapLakeStudyRefusal(candidate.refusal),
    dataSource: candidate.data_source ? mapLakeStudyDataSource(candidate.data_source) : null,
  };
}

// ---------------------------------------------------------------------------
// The shared data-source line
// ---------------------------------------------------------------------------

function resolutionText(barMinutes: number): string {
  return barMinutes === 1 ? '1-minute bars' : `${barMinutes}-minute bars, folded from 1-minute bars`;
}

function warmupText(sessions: number): string {
  return sessions === 0 ? 'none' : `${sessions} ${sessions === 1 ? 'session' : 'sessions'} before the window`;
}

/**
 * The server's facts as the shared data-source line prints them. The window bounds are dates (ET midnight), so the
 * host renders the line with `windowMode="date-et"`. Counts are the window's and its warmup's together: the sessions
 * this request needed.
 */
export function toDataSourceProvenance(source: LakeStudyDataSource): DataSourceProvenance {
  const { coverage } = source;
  return {
    sources: ['Data lake', source.provider],
    subject: source.symbol,
    requestedWindow: { startMs: source.windowStartMs, endMs: source.windowEndMs },
    effectiveWindow: source.effectiveWindow,
    warmup: warmupText(source.warmupSessions),
    resolution: resolutionText(source.barMinutes),
    adjustment: source.priceAdjustmentMode,
    sessionPolicy: source.sessionPolicy,
    dataIdentity: source.snapshotDigest,
    observedAtMs: null,
    counts:
      coverage === null
        ? null
        : {
            added: coverage.window.added + coverage.warmup.added,
            reused: coverage.window.reused + coverage.warmup.reused,
            missing: coverage.window.missing + coverage.warmup.missing,
          },
    limitations: [],
  };
}

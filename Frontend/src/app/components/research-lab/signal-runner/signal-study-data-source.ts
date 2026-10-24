import type { SignalStudyDataSource, StudyCoverageGroup } from '../../../services/research.service';

/** Session counts for one group, as the lake admission reports them. */
interface StudyCoverageGroupRaw {
  expected_sessions: number;
  reused: number;
  added: number;
  missing: number;
  failed: number;
  building: number;
  corrupt: number;
  unavailable_from_provider: number;
}

/** The ``data_source`` block of a Signal study result (``signal_study_service.data_source_block``). */
export interface SignalStudyDataSourceRaw {
  kind: 'lake';
  provider: string;
  price_adjustment_mode: string;
  bar_minutes: number;
  session_policy: 'extended' | 'regular';
  window_start_ms: number;
  window_end_ms: number;
  warmup_sessions: number;
  look_forward_bars: number;
  snapshot_digest: string | null;
  identity_digest: string | null;
  calculation_version: string;
  coverage: {
    window: StudyCoverageGroupRaw;
    warmup: StudyCoverageGroupRaw;
    artifacts: { fetched: number; reused: number; refreshed: number };
  } | null;
  bars_used: number | null;
  cache: { hit: boolean; cached_at_ms: number | null };
}

function mapGroup(g: StudyCoverageGroupRaw): StudyCoverageGroup {
  return {
    expectedSessions: g.expected_sessions,
    reused: g.reused,
    added: g.added,
    missing: g.missing,
    failed: g.failed,
    building: g.building,
    corrupt: g.corrupt,
    unavailableFromProvider: g.unavailable_from_provider,
  };
}

/** Snake-case worker block → camelCase view model. */
export function toSignalStudyDataSource(raw: SignalStudyDataSourceRaw): SignalStudyDataSource {
  return {
    kind: raw.kind,
    provider: raw.provider,
    priceAdjustmentMode: raw.price_adjustment_mode,
    barMinutes: raw.bar_minutes,
    sessionPolicy: raw.session_policy,
    windowStartMs: raw.window_start_ms,
    windowEndMs: raw.window_end_ms,
    warmupSessions: raw.warmup_sessions,
    lookForwardBars: raw.look_forward_bars,
    snapshotDigest: raw.snapshot_digest,
    identityDigest: raw.identity_digest,
    calculationVersion: raw.calculation_version,
    coverage: raw.coverage
      ? {
          window: mapGroup(raw.coverage.window),
          warmup: mapGroup(raw.coverage.warmup),
          artifacts: raw.coverage.artifacts,
        }
      : null,
    barsUsed: raw.bars_used,
    cache: { hit: raw.cache.hit, cachedAtMs: raw.cache.cached_at_ms },
  };
}

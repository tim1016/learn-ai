/**
 * Canonical ``DataPolicy`` wire contract — PR B (2026-05-19).
 *
 * Mirrors the Python ``app.lean_sidecar.data_policy.DataPolicy`` dataclass
 * and the .NET ``StrategyExecution.DataPolicyJson`` column. The shape is
 * backend-neutral: both the in-process engine path and the LEAN sidecar
 * path send the identical block, so the compare-view can gate on field
 * equality without normalizing between two vocabularies.
 *
 * See `docs/superpowers/specs/2026-05-19-pr-b-engine-lab-unified-design.md`
 * § 6.1 for the canonical example.
 */

/** Polygon-style (timespan, multiplier) pair carrying a single timeframe. */
export interface BarsSpec {
  timespan: 'minute' | 'hour' | 'day';
  multiplier: number;
}

/** Where the bars came from and what processing they went through. */
export interface DataPolicy {
  source: 'polygon' | 'synthetic';
  symbol: string;
  /** Staging-pipeline policy — NOT LEAN's DataNormalizationMode. */
  adjusted: boolean;
  session: 'regular' | 'extended';
  /** Raw input bars fetched from the provider. */
  input_bars: BarsSpec;
  /** Bars the strategy actually operates on (post-consolidation). */
  strategy_bars: BarsSpec;
  timestamp_policy: 'bar_close_ms_utc';
  timezone: 'America/New_York';
  provider_kind: 'live' | 'fixture';
  /** Populated only when provider_kind === 'fixture'. */
  fixture_id: string | null;
  /** Populated only when provider_kind === 'fixture'. */
  fixture_sha256: string | null;
}

/**
 * Rebuild a DataPolicy at the GraphQL-to-engine boundary.
 *
 * Apollo adds ``__typename`` to queried objects, including nested bar specs.
 * Explicit reconstruction keeps editable restored state identical to the
 * strict Python/LEAN request contract without weakening server validation.
 */
export function toDataPolicyPayload(policy: DataPolicy): DataPolicy {
  return {
    source: policy.source,
    symbol: policy.symbol,
    adjusted: policy.adjusted,
    session: policy.session,
    input_bars: {
      timespan: policy.input_bars.timespan,
      multiplier: policy.input_bars.multiplier,
    },
    strategy_bars: {
      timespan: policy.strategy_bars.timespan,
      multiplier: policy.strategy_bars.multiplier,
    },
    timestamp_policy: policy.timestamp_policy,
    timezone: policy.timezone,
    provider_kind: policy.provider_kind,
    fixture_id: policy.fixture_id,
    fixture_sha256: policy.fixture_sha256,
  };
}

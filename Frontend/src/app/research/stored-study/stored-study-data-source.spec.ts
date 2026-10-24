import { describe, expect, it } from 'vitest';

import { storedDataSource } from './stored-study-data-source';
import { storedRecord } from './testing/stored-study-fixtures';

const LAKE_BLOCK = {
  kind: 'lake',
  provider: 'polygon',
  symbol: 'SPY',
  price_adjustment_mode: 'polygon_split_adjusted',
  session_policy: 'extended',
  window_start_ms: 1_704_171_600_000,
  window_end_ms: 1_711_598_400_000,
  effective_window: { start_ms: 1_704_171_600_000, end_ms: 1_711_512_000_000 },
  warmup_sessions: 3,
  bar_minutes: 5,
  identity_digest: 'ident-1',
  snapshot_digest: 'snap-1',
  calculation_version: 'feature-study/1',
  cache: { hit: false, cached_at_ms: null },
};

describe('storedDataSource', () => {
  it('is “not recorded” for a run whose result carried no provenance, and never today’s provider', () => {
    expect(storedDataSource(storedRecord('feature-study'))).toBeNull();
  });

  it('reads the facts a lake study’s block holds and leaves the rest unrecorded', () => {
    const provenance = storedDataSource(storedRecord('feature-study', { data_source: LAKE_BLOCK as never }));

    expect(provenance).toMatchObject({
      sources: ['polygon'],
      subject: 'SPY',
      requestedWindow: { startMs: 1_704_171_600_000, endMs: 1_711_598_400_000 },
      effectiveWindow: { startMs: 1_704_171_600_000, endMs: 1_711_512_000_000 },
      warmup: '3 sessions',
      resolution: '5 minutes',
      adjustment: 'polygon_split_adjusted',
      sessionPolicy: 'extended',
      dataIdentity: 'snap-1',
      observedAtMs: null,
      counts: null,
    });
  });

  it('infers nothing from a block that names less: each missing fact stays null', () => {
    const provenance = storedDataSource(storedRecord('feature-study', { data_source: { kind: 'lake' } as never }));

    expect(provenance).toMatchObject({
      sources: ['lake'],
      subject: null,
      requestedWindow: null,
      effectiveWindow: null,
      warmup: null,
      resolution: null,
      dataIdentity: null,
    });
  });

  it('names the digest of the bars a run carried as its data identity', () => {
    const provenance = storedDataSource(storedRecord('realized-vs-iv', { data_identity: 'f17a0b24' }));

    expect(provenance).toMatchObject({ sources: [], dataIdentity: 'f17a0b24' });
  });
});

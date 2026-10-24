import { describe, expect, it } from 'vitest';

import {
  mapLakeStudyDataSource,
  readLakeStudyRefusal,
  sessionPolicyWords,
  toDataSourceProvenance,
  type LakeCoverageGroupRaw,
  type LakeDataSourceRaw,
} from './lake-study';

const MONDAY = Date.UTC(2024, 2, 4, 5, 0); // 2024-03-04 00:00 ET
const FRIDAY = Date.UTC(2024, 2, 8, 5, 0);

function group(over: Partial<LakeCoverageGroupRaw> = {}): LakeCoverageGroupRaw {
  return {
    expected_sessions: 5, reused: 0, added: 5, missing: 0, failed: 0,
    building: 0, corrupt: 0, captured_before_final: 0, unavailable_from_provider: 0, ...over,
  };
}

/** The block `data_source_block` writes for a completed run, as the server sends it. */
function block(over: Partial<LakeDataSourceRaw> = {}): LakeDataSourceRaw {
  return {
    kind: 'lake',
    provider: 'polygon',
    symbol: 'SPY',
    price_adjustment_mode: 'polygon_split_adjusted',
    session_policy: 'extended',
    window_start_ms: MONDAY,
    window_end_ms: FRIDAY,
    effective_window: { start_ms: MONDAY, end_ms: FRIDAY },
    warmup_sessions: 0,
    bar_minutes: 5,
    aggregation: 'minute_fold_et_wall_clock_no_fill',
    identity_digest: 'a'.repeat(64),
    calculation_version: 'feature-study/1',
    snapshot_digest: 'b'.repeat(64),
    coverage: {
      window: group({ reused: 2, added: 3 }),
      warmup: group({ expected_sessions: 0, added: 0 }),
      artifacts: { fetched: 3, reused: 2, refreshed: 0 },
    },
    bars_used: 1920,
    cache: { hit: false, cached_at_ms: null },
    ...over,
  };
}

describe('lake study data source', () => {
  it('maps the block to camelCase without inventing a fact', () => {
    const source = mapLakeStudyDataSource(block());

    expect(source).toMatchObject({
      symbol: 'SPY', priceAdjustmentMode: 'polygon_split_adjusted', sessionPolicy: 'extended', barMinutes: 5,
      snapshotDigest: 'b'.repeat(64), barsUsed: 1920, effectiveWindow: { startMs: MONDAY, endMs: FRIDAY },
    });
    expect(source.coverage?.window).toMatchObject({ expectedSessions: 5, reused: 2, added: 3, unavailableFromProvider: 0, capturedBeforeFinal: 0 });
    expect(source.cache).toEqual({ hit: false, cachedAtMs: null });
  });

  it('keeps an unrecorded cache time and an absent effective window as null, never zero', () => {
    const source = mapLakeStudyDataSource(block({ effective_window: null, cache: { hit: true, cached_at_ms: null } }));

    expect(source.effectiveWindow).toBeNull();
    expect(source.cache).toEqual({ hit: true, cachedAtMs: null });
  });

  it('projects onto the shared data-source line: lake and provider, the window read, the bars, the counts', () => {
    const line = toDataSourceProvenance(mapLakeStudyDataSource(block()));

    expect(line).toMatchObject({
      sources: ['Data lake', 'polygon'], subject: 'SPY', warmup: 'none',
      resolution: '5-minute bars, folded from 1-minute bars', adjustment: 'polygon_split_adjusted',
      sessionPolicy: 'extended', dataIdentity: 'b'.repeat(64), observedAtMs: null,
      requestedWindow: { startMs: MONDAY, endMs: FRIDAY }, effectiveWindow: { startMs: MONDAY, endMs: FRIDAY },
      counts: { added: 3, reused: 2, missing: 0 },
    });
  });

  it('adds the warmup sessions to the counts and words them', () => {
    const line = toDataSourceProvenance(
      mapLakeStudyDataSource(block({
        warmup_sessions: 3, bar_minutes: 1,
        coverage: {
          window: group({ reused: 5, added: 0 }),
          warmup: group({ expected_sessions: 3, reused: 1, added: 2 }),
          artifacts: { fetched: 2, reused: 6, refreshed: 0 },
        },
      })),
    );

    expect(line.warmup).toBe('3 sessions before the window');
    expect(line.resolution).toBe('1-minute bars');
    expect(line.counts).toEqual({ added: 2, reused: 6, missing: 0 });
  });

  it('prints no window, counts or identity the server did not record', () => {
    const line = toDataSourceProvenance(
      mapLakeStudyDataSource(block({ effective_window: null, coverage: null, snapshot_digest: null })),
    );

    expect(line.effectiveWindow).toBeNull();
    expect(line.counts).toBeNull();
    expect(line.dataIdentity).toBeNull();
  });
});

describe('lake study refusal payload', () => {
  const refused = {
    success: false,
    status: 'refused',
    ticker: 'SPY',
    error: 'The lake cannot admit this window: 1 of 5 sessions cannot be supplied by the provider.',
    refusal: {
      code: 'data_unavailable_from_provider',
      reason: 'The lake cannot admit this window: 1 of 5 sessions cannot be supplied by the provider.',
      sessions: [{ group: 'window', trading_date_ms: Date.UTC(2024, 2, 6, 14, 30), outcome: 'unavailable_from_provider', reason: 'provider_no_data', failure_reason: 'provider_no_data' }],
      sessions_total: 1,
      coverage: {
        window: group({ added: 4, failed: 1, unavailable_from_provider: 1 }),
        warmup: group({ expected_sessions: 0, added: 0 }),
        artifacts: { fetched: 4, reused: 0, refreshed: 0 },
      },
    },
    data_source: block({ snapshot_digest: null, effective_window: null }),
  };

  it('reads the refusal a failed job parked, with the source it was refused against', () => {
    const result = readLakeStudyRefusal(refused);

    expect(result?.refusal).toMatchObject({ code: 'data_unavailable_from_provider', sessionsTotal: 1 });
    expect(result?.refusal.sessions[0]).toMatchObject({ outcome: 'unavailable_from_provider', group: 'window', failureReason: 'provider_no_data' });
    expect(result?.refusal.coverage?.window).toMatchObject({ added: 4, failed: 1 });
    expect(result?.dataSource?.symbol).toBe('SPY');
  });

  it('reads a refusal made before anything was read as one with no source', () => {
    const result = readLakeStudyRefusal({ ...refused, data_source: null, refusal: { ...refused.refusal, coverage: null, sessions: [] } });

    expect(result?.dataSource).toBeNull();
    expect(result?.refusal.coverage).toBeNull();
  });

  it.each([
    ['nothing', null],
    ['a number', 7],
    ['a completed result', { success: true, mean_ic: 0.1 }],
    ['a refusal status without a refusal', { status: 'refused' }],
  ])('is not a refusal when the payload is %s', (_label, payload) => {
    expect(readLakeStudyRefusal(payload)).toBeNull();
  });
});

describe('the session a request asks for', () => {
  it('words each session for a reader', () => {
    expect.soft(sessionPolicyWords('regular')).toBe('regular hours');
    expect.soft(sessionPolicyWords('extended')).toBe('extended hours');
  });
});

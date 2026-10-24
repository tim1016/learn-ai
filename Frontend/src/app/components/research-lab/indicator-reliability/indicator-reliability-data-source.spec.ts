import { HttpErrorResponse } from '@angular/common/http';

import {
  describeReliabilitySource,
  refusalDataSource,
  type ReliabilityDataSource,
} from './indicator-reliability-data-source';

const group = (over: Partial<NonNullable<ReliabilityDataSource['coverage']>['window']> = {}) => ({
  expected_sessions: 11,
  reused: 8,
  added: 3,
  missing: 0,
  failed: 0,
  building: 0,
  corrupt: 0,
  unavailable_from_provider: 0,
  ...over,
});

function source(over: Partial<ReliabilityDataSource> = {}): ReliabilityDataSource {
  return {
    kind: 'lake',
    provider: 'polygon',
    price_adjustment_mode: 'polygon_split_adjusted',
    session_policy: 'regular',
    bar_minutes: 1,
    aggregation: 'minute_fold_et_wall_clock_no_fill',
    window_start_ms: 1709528400000,
    window_end_ms: 1710478800000,
    calculation_version: 'indicator-reliability/1',
    warmup: {
      policy: 'allow_shortened',
      requested_bars: 140,
      requested_sessions: 1,
      admitted_sessions: 1,
      admitted_bars: 390,
      shortened: false,
      shortfall_bars: 0,
      cause: 'none',
    },
    coverage: {
      window: group(),
      warmup: group({ expected_sessions: 1, reused: 0, added: 1 }),
      artifacts: { fetched: 4, reused: 8, refreshed: 0 },
    },
    ...over,
  };
}

describe('describeReliabilitySource', () => {
  it('names the lake bars and counts distinct sessions reused, added, missing and failed, warmup apart', () => {
    const line = describeReliabilitySource(source());

    expect(line.method).toBe('Polygon · split-adjusted · regular hours · 1-min bars');
    expect(line.freshness).toBe(
      'window 11 sessions: 8 reused · 3 added · 0 missing · 0 failed · warmup 1 session: 0 reused · 1 added · 0 missing · 0 failed',
    );
    expect(line.notice).toBeNull();
  });

  it('says an hourly study reads one-hour bars and a study with no lead-in leaves the warmup out of the line', () => {
    const line = describeReliabilitySource(
      source({
        bar_minutes: 60,
        coverage: {
          window: group(),
          warmup: group({ expected_sessions: 0, reused: 0, added: 0 }),
          artifacts: { fetched: 0, reused: 0, refreshed: 0 },
        },
      }),
    );

    expect(line.method).toContain('1-hour bars');
    expect(line.freshness).not.toContain('warmup');
  });

  it('warns, with the cause in words, when the warmup was cut short of what the policy asks', () => {
    const line = describeReliabilitySource(
      source({
        warmup: {
          policy: 'allow_shortened',
          requested_bars: 1000,
          requested_sessions: 3,
          admitted_sessions: 1,
          admitted_bars: 390,
          shortened: true,
          shortfall_bars: 610,
          cause: 'provider_history_floor',
        },
      }),
    );

    expect(line.notice).toBe(
      "Warmup is 610 bars short of the 1000 the policy asks for (the provider's history does not reach back further); " +
        "the indicator's first values may be less settled.",
    );
  });

  it('reports a refused run that admitted nothing as not admitted', () => {
    expect(describeReliabilitySource(source({ coverage: null, warmup: null })).freshness).toBe('not admitted');
  });
});

describe('refusalDataSource', () => {
  const refusal = (body: unknown) => new HttpErrorResponse({ status: 409, error: body });

  it('reads the data source a refused run carries under FastAPI\'s detail envelope', () => {
    const found = refusalDataSource(refusal({ detail: { code: 'data_unavailable_from_provider', message: 'x', data_source: source() } }));

    expect(found?.coverage?.window.expected_sessions).toBe(11);
  });

  it('finds nothing in a plain-text error or a body without a data source', () => {
    expect(refusalDataSource(refusal({ detail: 'Maximum horizon is 100 bars' }))).toBeNull();
    expect(refusalDataSource(refusal({ detail: { code: 'resolution_unsupported', message: 'x', data_source: null } }))).toBeNull();
    expect(refusalDataSource(new Error('offline'))).toBeNull();
  });
});

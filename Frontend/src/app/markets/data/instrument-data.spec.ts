import { describe, expect, it } from 'vitest';

import type { InstrumentDataReport } from '../../shared/data-lake';
import { fillReport, gapStretches, type InstrumentSession } from './instrument-data';

const DAY = 86_400_000;
const T0 = Date.UTC(2024, 4, 20, 13, 30);

/** A session `n` calendar days from the first; the spec does not need the calendar, only that neighbours are listed adjacent. */
function session(n: number, state: InstrumentSession['state'], extra: Partial<InstrumentSession> = {}): InstrumentSession {
  return { trading_date_ms: T0 + n * DAY, state, ...extra };
}

function report(sessions: InstrumentSession[], runReadable = false): InstrumentDataReport {
  return {
    market: 'usa',
    symbol: 'SPY',
    data_type: 'trade',
    resolution: 'minute',
    price_adjustment_mode: 'raw',
    data_root_id: '00000000-0000-0000-0000-000000000000',
    provider_history_start_ms: T0 - 1000 * DAY,
    as_of_ms: T0 + 100 * DAY,
    summary: {
      calendar_day_count: sessions.length,
      session_count: sessions.length,
      ready: sessions.filter((s) => s.state === 'ready').length,
      building: 0,
      needs_rebuild: 0,
      missing: 0,
      failed: 0,
      forming: 0,
      upcoming: 0,
      fetchable: 0,
      before_provider_history: 0,
      provider_no_data: 0,
      run_readable: runReadable,
    },
    sessions,
    fill_window: null,
    daily_rollup: { state: 'current' },
  };
}

describe('gapStretches', () => {
  it('groups neighbouring sessions that share a cause, and splits them at a held session', () => {
    const stretches = gapStretches([
      session(0, 'missing', { fetchable: true }),
      session(1, 'missing', { fetchable: true }),
      session(2, 'ready'),
      session(3, 'missing', { fetchable: true }),
    ]);

    expect(stretches.map((s) => [s.firstMs, s.lastMs, s.count])).toEqual([
      [T0, T0 + DAY, 2],
      [T0 + 3 * DAY, T0 + 3 * DAY, 1],
    ]);
  });

  it('keeps a different cause its own stretch, so a retryable failure never hides inside a refused one', () => {
    const stretches = gapStretches([
      session(0, 'missing', { fetchable: false, blocked_by: 'before_provider_history' }),
      session(1, 'missing', { fetchable: true }),
      session(2, 'failed', { reason: 'provider_no_data', blocked_by: 'provider_no_data' }),
      session(3, 'failed', { reason: 'provider_api_error', fetchable: true }),
    ]);

    expect(stretches.map((s) => [s.state, s.reason, s.fetchable, s.blockedBy, s.count])).toEqual([
      ['missing', null, false, 'before_provider_history', 1],
      ['missing', null, true, null, 1],
      ['failed', 'provider_no_data', false, 'provider_no_data', 1],
      ['failed', 'provider_api_error', true, null, 1],
    ]);
  });

  it('lists no gap for sessions that are ready or not finished yet, and ends a stretch at them', () => {
    expect(gapStretches([session(0, 'ready'), session(1, 'forming'), session(2, 'upcoming')])).toEqual([]);
    const around = gapStretches([session(0, 'missing', { fetchable: true }), session(1, 'forming'), session(2, 'missing', { fetchable: true })]);
    expect(around).toHaveLength(2);
  });
});

describe('fillReport', () => {
  const before = report([
    session(0, 'ready'),
    session(1, 'ready'),
    session(2, 'missing', { fetchable: true }),
    session(3, 'needs_rebuild', { fetchable: true, reason: 'adjustment_record_missing' }),
    session(4, 'missing', { fetchable: false, blocked_by: 'before_provider_history' }),
  ]);

  it('counts what was added, what was already held, and what is still missing with its cause', () => {
    const after = report([
      session(0, 'ready'),
      session(1, 'ready'),
      session(2, 'ready'),
      session(3, 'ready'),
      session(4, 'missing', { fetchable: false, blocked_by: 'before_provider_history' }),
    ]);

    const result = fillReport(before, after, { kind: 'completed' });

    expect(result.added).toBe(2);
    expect(result.reused).toBe(2);
    expect(result.missingCount).toBe(1);
    expect(result.missing).toEqual([
      { state: 'missing', reason: null, fetchable: false, blockedBy: 'before_provider_history', count: 1 },
    ]);
    expect(result.complete, 'a session the provider no longer serves keeps the fill from reading as complete').toBe(false);
  });

  it('is complete only when the job finished and a run can read every finished session', () => {
    const allReady = report([session(0, 'ready'), session(1, 'ready')], true);

    expect(fillReport(before, allReady, { kind: 'completed' }).complete).toBe(true);
    expect(
      fillReport(before, allReady, { kind: 'failed', code: 'backfill_run_error', message: 'boom' }).complete,
      'a fill that stopped never reads as complete, whatever the lake holds afterwards',
    ).toBe(false);
  });

  it('groups what is still missing by cause and counts each cause once', () => {
    const after = report([
      session(0, 'ready'),
      session(1, 'failed', { reason: 'provider_api_error', fetchable: true }),
      session(2, 'failed', { reason: 'provider_api_error', fetchable: true }),
      session(3, 'failed', { reason: 'provider_no_data', blocked_by: 'provider_no_data' }),
    ]);

    const result = fillReport(before, after, { kind: 'completed' });

    expect(result.missingCount).toBe(3);
    expect(result.missing.map((cause) => [cause.reason, cause.count])).toEqual([
      ['provider_api_error', 2],
      ['provider_no_data', 1],
    ]);
  });

  it('counts a session another run finished meanwhile as added to this window, never twice', () => {
    const after = report([session(0, 'ready'), session(1, 'ready'), session(2, 'ready'), session(3, 'ready'), session(4, 'ready')], true);

    const result = fillReport(before, after, { kind: 'completed' });

    expect(result.added + result.reused).toBe(5);
  });
});

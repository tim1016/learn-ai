import { describe, expect, it } from 'vitest';

import { dataReport, dataSession } from '../../shared/data-lake/testing/instrument-data-report';
import { fillReport, gapStretches } from './instrument-data';

const DAY = 86_400_000;
/** The first session of the shared builder's window; the spec does not need the calendar, only that neighbours are listed adjacent. */
const T0 = dataSession(0, 'ready').trading_date_ms;

describe('gapStretches', () => {
  it('groups neighbouring sessions that share a cause, and splits them at a held session', () => {
    const stretches = gapStretches([
      dataSession(0, 'missing', { fetchable: true }),
      dataSession(1, 'missing', { fetchable: true }),
      dataSession(2, 'ready'),
      dataSession(3, 'missing', { fetchable: true }),
    ]);

    expect(stretches.map((s) => [s.firstMs, s.lastMs, s.count])).toEqual([
      [T0, T0 + DAY, 2],
      [T0 + 3 * DAY, T0 + 3 * DAY, 1],
    ]);
  });

  it('keeps a different cause its own stretch, so a retryable failure never hides inside a refused one', () => {
    const stretches = gapStretches([
      dataSession(0, 'missing', { fetchable: false, blocked_by: 'before_provider_history' }),
      dataSession(1, 'missing', { fetchable: true }),
      dataSession(2, 'failed', { reason: 'provider_no_data', blocked_by: 'provider_no_data' }),
      dataSession(3, 'failed', { reason: 'provider_api_error', fetchable: true }),
    ]);

    expect(stretches.map((s) => [s.state, s.reason, s.fetchable, s.blockedBy, s.count])).toEqual([
      ['missing', null, false, 'before_provider_history', 1],
      ['missing', null, true, null, 1],
      ['failed', 'provider_no_data', false, 'provider_no_data', 1],
      ['failed', 'provider_api_error', true, null, 1],
    ]);
  });

  it('lists no gap for sessions that are ready or not finished yet, and ends a stretch at them', () => {
    expect(gapStretches([dataSession(0, 'ready'), dataSession(1, 'forming'), dataSession(2, 'upcoming')])).toEqual([]);
    const around = gapStretches([dataSession(0, 'missing', { fetchable: true }), dataSession(1, 'forming'), dataSession(2, 'missing', { fetchable: true })]);
    expect(around).toHaveLength(2);
  });
});

describe('fillReport', () => {
  const before = dataReport([
    dataSession(0, 'ready'),
    dataSession(1, 'ready'),
    dataSession(2, 'missing', { fetchable: true }),
    dataSession(3, 'needs_rebuild', { fetchable: true, reason: 'adjustment_record_missing' }),
    dataSession(4, 'missing', { fetchable: false, blocked_by: 'before_provider_history' }),
  ]);

  it('counts what was added, what was already held, and what is still missing with its cause', () => {
    const after = dataReport([
      dataSession(0, 'ready'),
      dataSession(1, 'ready'),
      dataSession(2, 'ready'),
      dataSession(3, 'ready'),
      dataSession(4, 'missing', { fetchable: false, blocked_by: 'before_provider_history' }),
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
    const allReady = dataReport([dataSession(0, 'ready'), dataSession(1, 'ready')]);

    expect(fillReport(before, allReady, { kind: 'completed' }).complete).toBe(true);
    expect(
      fillReport(before, allReady, { kind: 'failed', code: 'backfill_run_error', message: 'boom' }).complete,
      'a fill that stopped never reads as complete, whatever the lake holds afterwards',
    ).toBe(false);
  });

  it('groups what is still missing by cause and counts each cause once', () => {
    const after = dataReport([
      dataSession(0, 'ready'),
      dataSession(1, 'failed', { reason: 'provider_api_error', fetchable: true }),
      dataSession(2, 'failed', { reason: 'provider_api_error', fetchable: true }),
      dataSession(3, 'failed', { reason: 'provider_no_data', blocked_by: 'provider_no_data' }),
    ]);

    const result = fillReport(before, after, { kind: 'completed' });

    expect(result.missingCount).toBe(3);
    expect(result.missing.map((cause) => [cause.reason, cause.count])).toEqual([
      ['provider_api_error', 2],
      ['provider_no_data', 1],
    ]);
  });

  it('counts a session another run finished meanwhile as added to this window, never twice', () => {
    const after = dataReport([dataSession(0, 'ready'), dataSession(1, 'ready'), dataSession(2, 'ready'), dataSession(3, 'ready'), dataSession(4, 'ready')]);

    const result = fillReport(before, after, { kind: 'completed' });

    expect(result.added + result.reused).toBe(5);
  });
});

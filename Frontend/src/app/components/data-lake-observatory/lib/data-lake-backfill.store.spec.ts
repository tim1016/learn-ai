import { HttpErrorResponse } from '@angular/common/http';
import { TestBed } from '@angular/core/testing';
import { describe, expect, it, vi } from 'vitest';

import { JobsService } from '../../../services/jobs.service';
import { BACKFILL_JOB_TYPE, DataLakeBackfillStore } from './data-lake-backfill.store';
import type { DataRunSpec } from './data-lake.types';

/** 09:30 America/New_York on 2026-05-20, as int64 ms UTC. */
const MAY_20_OPEN_MS = Date.UTC(2026, 4, 20, 13, 30);

const SPEC: DataRunSpec = {
  request_id: '11111111-2222-3333-4444-555555555555',
  run_type: 'python_lab',
  market: 'usa',
  symbols: ['SPY'],
  start_trading_date: '2026-05-20',
  end_trading_date: '2026-05-20',
  data_types: ['trade'],
  lean_image_digest: 'sha256:pinned',
  force_refresh: false,
};

// The store rides JobsService.onEvent() (#1856) rather than opening its own
// EventSource, so every mocked JobsService below needs a no-op onEvent —
// start()/reattach() call it to register the store's fold as a listener,
// and jsdom has no EventSource for the real service to construct anyway.
// The store only ever parses a frame and routes it into `ingestEvent`,
// which every test below drives directly.
function makeStore(jobs: Partial<JobsService>): DataLakeBackfillStore {
  TestBed.configureTestingModule({
    providers: [
      DataLakeBackfillStore,
      { provide: JobsService, useValue: { onEvent: vi.fn().mockReturnValue(vi.fn()), ...jobs } },
    ],
  });
  return TestBed.inject(DataLakeBackfillStore);
}

describe('DataLakeBackfillStore', () => {
  it('submits under the public job type the jobs framework routes', async () => {
    const startJob = vi.fn().mockResolvedValue('job-1');
    const store = makeStore({ startJob } as unknown as Partial<JobsService>);

    await store.start(SPEC);

    expect(startJob).toHaveBeenCalledWith(BACKFILL_JOB_TYPE, { spec: SPEC });
    expect(store.jobId()).toBe('job-1');
    expect(store.phase()).toBe('running');
    expect(store.reattached()).toBe(false);
  });

  it('adopts a run the server is already executing', () => {
    const store = makeStore({} as Partial<JobsService>);

    store.reattach('job-live');

    expect(store.jobId()).toBe('job-live');
    expect(store.phase()).toBe('running');
    expect(store.running()).toBe(true);
    // Named as adopted, so the panel can say the history below was
    // replayed rather than observed from the start.
    expect(store.reattached()).toBe(true);
  });

  it('rebuilds an adopted run from the replayed stream rather than inventing it', () => {
    const store = makeStore({} as Partial<JobsService>);
    store.reattach('job-live');

    // What GET /api/jobs/{id}/events replays when opened with no
    // Last-Event-ID: the whole stream from the start.
    store.ingestEvent({ type: 'job.started' });
    store.ingestEvent({ type: 'job.progress', current: 2, total: 3, unit: 'days' });
    for (const dayIndex of [1, 2]) {
      store.ingestEvent({
        type: 'data_lake.backfill_day',
        trading_date_ms: MAY_20_OPEN_MS + dayIndex * 86_400_000,
        day_index: dayIndex,
        total_days: 3,
        days_remaining: 3 - dayIndex,
        fetched_count: 1,
        reused_count: 0,
        failures: [],
      });
    }

    expect(store.days()).toHaveLength(2);
    expect(store.fetchedCount()).toBe(2);
    expect(store.progress()).toMatchObject({ current: 2, total: 3 });
  });

  it('reaches its terminal phase after adopting, so the caller can re-read', () => {
    const store = makeStore({} as Partial<JobsService>);
    store.reattach('job-live');

    store.ingestEvent({ type: 'job.completed' });

    expect(store.phase()).toBe('completed');
  });

  it('ignores a re-adopt of the run it is already following', () => {
    const store = makeStore({} as Partial<JobsService>);
    store.reattach('job-live');
    store.ingestEvent({ type: 'job.progress', current: 1, total: 2, unit: 'days' });

    store.reattach('job-live');

    expect(store.progress()).toMatchObject({ current: 1 });
  });

  it('clears the adopted flag when a fresh run is submitted', async () => {
    const startJob = vi.fn().mockResolvedValue('job-2');
    const store = makeStore({ startJob } as unknown as Partial<JobsService>);
    store.reattach('job-live');

    await store.start(SPEC);

    expect(store.jobId()).toBe('job-2');
    expect(store.reattached()).toBe(false);
  });

  it('names a refused submission as "not enabled" when the route is dark', async () => {
    const startJob = vi
      .fn()
      .mockRejectedValue(new HttpErrorResponse({ status: 404, statusText: 'Not Found' }));
    const store = makeStore({ startJob } as unknown as Partial<JobsService>);

    await store.start(SPEC);

    expect(store.phase()).toBe('failed');
    expect(store.error()).toEqual({
      code: 'data_lake_not_enabled',
      message: 'The data plane refused the backfill: the data lake is not enabled.',
    });
  });

  it("carries a typed rejection's own reason code onto the run", async () => {
    const startJob = vi.fn().mockRejectedValue(
      new HttpErrorResponse({
        status: 422,
        statusText: 'Unprocessable Entity',
        error: { detail: { reason: 'range_too_large', message: 'range is 3654 days' } },
      }),
    );
    const store = makeStore({ startJob } as unknown as Partial<JobsService>);

    await store.start(SPEC);

    expect(store.error()).toEqual({ code: 'range_too_large', message: 'range is 3654 days' });
  });

  it('folds a per-day domain event into the run, typed failures intact', () => {
    const store = makeStore({} as Partial<JobsService>);

    store.ingestEvent({
      type: 'data_lake.backfill_day',
      trading_date_ms: MAY_20_OPEN_MS,
      day_index: 1,
      total_days: 2,
      days_remaining: 1,
      fetched_count: 1,
      reused_count: 0,
      failures: [
        {
          artifact_kind: 'minute_trade',
          symbol: 'SPY',
          trading_date_ms: MAY_20_OPEN_MS,
          data_type: 'trade',
          reason: 'provider_entitlement_error',
          detail: 'plan does not include this feed',
          provider_status_code: 403,
          attempt_count: 1,
        },
      ],
    });

    expect(store.days()).toHaveLength(1);
    expect(store.failures()[0].reason).toBe('provider_entitlement_error');
    expect(store.fetchedCount()).toBe(1);
  });

  it('corrects a redelivered day in place instead of double-counting it', () => {
    const store = makeStore({} as Partial<JobsService>);
    const day = {
      type: 'data_lake.backfill_day',
      trading_date_ms: MAY_20_OPEN_MS,
      day_index: 1,
      total_days: 1,
      days_remaining: 0,
      fetched_count: 1,
      reused_count: 0,
      failures: [],
    };

    store.ingestEvent(day);
    store.ingestEvent({ ...day, fetched_count: 2 });

    expect(store.days()).toHaveLength(1);
    expect(store.fetchedCount()).toBe(2);
  });

  it('tracks progress ticks and the terminal completion', () => {
    const store = makeStore({} as Partial<JobsService>);

    store.ingestEvent({ type: 'job.progress', current: 3, total: 5, unit: 'days' });
    store.ingestEvent({ type: 'job.completed' });

    expect(store.progress()).toMatchObject({ current: 3, total: 5, unit: 'days' });
    expect(store.phase()).toBe('completed');
    expect(store.running()).toBe(false);
  });

  it('keeps the failure code the job reported', () => {
    const store = makeStore({} as Partial<JobsService>);

    store.ingestEvent({ type: 'job.failed', code: 'PythonRejected', message: 'boom' });

    expect(store.error()).toEqual({ code: 'PythonRejected', message: 'boom' });
    expect(store.phase()).toBe('failed');
  });

  it('ignores an event type it does not know', () => {
    const store = makeStore({} as Partial<JobsService>);

    store.ingestEvent({ type: 'something.else' });

    expect(store.phase()).toBe('idle');
  });

  // ── JobsService.onEvent wiring (#1856) — no second EventSource ──────

  it('start() rides JobsService.onEvent() instead of opening its own stream', async () => {
    const startJob = vi.fn().mockResolvedValue('job-1');
    const onEvent = vi.fn().mockReturnValue(vi.fn());
    const store = makeStore({ startJob, onEvent } as unknown as Partial<JobsService>);

    await store.start(SPEC);

    expect(onEvent).toHaveBeenCalledWith('job-1', expect.any(Function));
  });

  it('a frame delivered through the registered handler folds the same as a direct ingestEvent() call', async () => {
    let handler: ((event: { type: string } & Record<string, unknown>) => void) | undefined;
    const startJob = vi.fn().mockResolvedValue('job-1');
    const onEvent = vi.fn((_jobId: string, h: typeof handler) => {
      handler = h;
      return vi.fn();
    });
    const store = makeStore({ startJob, onEvent } as unknown as Partial<JobsService>);

    await store.start(SPEC);
    handler?.({ type: 'job.progress', current: 1, total: 2, unit: 'days' });

    expect(store.progress()).toMatchObject({ current: 1, total: 2 });
  });

  it('unsubscribes once a terminal event is folded', async () => {
    let handler: ((event: { type: string } & Record<string, unknown>) => void) | undefined;
    const unsubscribe = vi.fn();
    const startJob = vi.fn().mockResolvedValue('job-1');
    const onEvent = vi.fn((_jobId: string, h: typeof handler) => {
      handler = h;
      return unsubscribe;
    });
    const store = makeStore({ startJob, onEvent } as unknown as Partial<JobsService>);

    await store.start(SPEC);
    handler?.({ type: 'job.completed' });

    expect(unsubscribe).toHaveBeenCalledTimes(1);
  });

  it('reattach() also rides JobsService.onEvent() for the adopted job', () => {
    const onEvent = vi.fn().mockReturnValue(vi.fn());
    const store = makeStore({ onEvent } as unknown as Partial<JobsService>);

    store.reattach('job-live');

    expect(onEvent).toHaveBeenCalledWith('job-live', expect.any(Function));
  });
});

import { HttpErrorResponse } from '@angular/common/http';
import { TestBed } from '@angular/core/testing';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

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

// jsdom has no EventSource; the store only ever parses a frame and routes
// it into `ingestEvent`, so a stub constructor is enough to let `start()`
// run and the fold be driven directly.
let originalEventSource: typeof EventSource;

function installEventSourceStub(): void {
  originalEventSource = globalThis.EventSource;
  class StubEventSource {
    onmessage: ((event: MessageEvent<string>) => void) | null = null;
    onerror: (() => void) | null = null;
    close(): void {}
  }
  (globalThis as unknown as { EventSource: typeof EventSource }).EventSource =
    StubEventSource as unknown as typeof EventSource;
}

function makeStore(jobs: Partial<JobsService>): DataLakeBackfillStore {
  TestBed.configureTestingModule({
    providers: [DataLakeBackfillStore, { provide: JobsService, useValue: jobs }],
  });
  return TestBed.inject(DataLakeBackfillStore);
}

describe('DataLakeBackfillStore', () => {
  beforeEach(() => installEventSourceStub());

  afterEach(() => {
    (globalThis as unknown as { EventSource: typeof EventSource }).EventSource =
      originalEventSource;
  });

  it('submits under the public job type the jobs framework routes', async () => {
    const startJob = vi.fn().mockResolvedValue('job-1');
    const store = makeStore({ startJob } as unknown as Partial<JobsService>);

    await store.start(SPEC);

    expect(startJob).toHaveBeenCalledWith(BACKFILL_JOB_TYPE, { spec: SPEC });
    expect(store.jobId()).toBe('job-1');
    expect(store.phase()).toBe('running');
  });

  it('names a refused submission as "not enabled" when the route is dark', async () => {
    const startJob = vi
      .fn()
      .mockRejectedValue(new HttpErrorResponse({ status: 404, statusText: 'Not Found' }));
    const store = makeStore({ startJob } as unknown as Partial<JobsService>);

    await store.start(SPEC);

    expect(store.notEnabled()).toBe(true);
    expect(store.phase()).toBe('failed');
    expect(store.error()?.code).toBe('data_lake_not_enabled');
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
});

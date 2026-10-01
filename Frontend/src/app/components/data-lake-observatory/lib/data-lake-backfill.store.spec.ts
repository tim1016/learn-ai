import { HttpErrorResponse, provideHttpClient } from '@angular/common/http';
import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { TestBed } from '@angular/core/testing';
import { signal } from '@angular/core';
import { describe, expect, it, vi } from 'vitest';

import { JobsService } from '../../../services/jobs.service';
import { BACKFILL_JOB_TYPE, DataLakeBackfillStore } from './data-lake-backfill.store';
import { BackfillJobRunner } from '../../../shared/data-lake/backfill-job-runner';
import type { DataRunSpec } from '../../../shared/data-lake';

interface ControllableEventSource {
  dispatch(payload: Record<string, unknown>, lastEventId?: string): void;
}

/** Replaces the browser's EventSource with a stub the test drives (#2472). */
function installEventSourceStub(): { last: () => ControllableEventSource | null; restore: () => void } {
  const originalEventSource = globalThis.EventSource;
  let lastSource: ControllableEventSource | null = null;
  const setLastSource = (instance: ControllableEventSource): void => {
    lastSource = instance;
  };
  class StubEventSource {
    onmessage: ((ev: { data: string; lastEventId?: string }) => void) | null = null;
    onerror: (() => void) | null = null;
    constructor() {
      setLastSource(this);
    }
    close(): void {
      // The service's close() flips its own bookkeeping.
    }
    dispatch(payload: Record<string, unknown>, lastEventId = ''): void {
      this.onmessage?.({ data: JSON.stringify(payload), lastEventId });
    }
  }
  (globalThis as unknown as { EventSource: typeof EventSource }).EventSource =
    StubEventSource as unknown as typeof EventSource;
  return {
    last: () => lastSource,
    restore: () => {
      (globalThis as unknown as { EventSource: typeof EventSource }).EventSource = originalEventSource;
    },
  };
}

/** 09:30 America/New_York on 2026-05-20, as int64 ms UTC. */
const MAY_20_OPEN_MS = Date.UTC(2026, 4, 20, 13, 30);
/** 12:00:00.000 UTC on 2026-05-20 — DataRunSpec's calendar anchor (#1877). */
const MAY_20_CALENDAR_ANCHOR_MS = Date.UTC(2026, 4, 20, 12, 0);

const SPEC: DataRunSpec = {
  request_id: '11111111-2222-3333-4444-555555555555',
  run_type: 'python_lab',
  market: 'usa',
  symbols: ['SPY'],
  start_trading_date_ms: MAY_20_CALENDAR_ANCHOR_MS,
  end_trading_date_ms: MAY_20_CALENDAR_ANCHOR_MS,
  data_types: ['trade'],
  lean_image_digest: 'sha256:pinned',
};

// The store rides JobsService.onEvent() (#1856) rather than opening its own
// EventSource, so every mocked JobsService below needs a no-op onEvent —
// start()/reattach() call it to register the runner's lifecycle fold,
// and jsdom has no EventSource for the real service to construct anyway.
// Domain-frame tests drive `ingestEvent` directly; lifecycle tests invoke
// the captured listener so they exercise the runner's one `job.*` fold.
function makeStore(jobs: Partial<JobsService>): DataLakeBackfillStore {
  TestBed.configureTestingModule({
    providers: [
      DataLakeBackfillStore,
      {
        provide: JobsService,
        useValue: {
          onEvent: vi.fn().mockReturnValue(vi.fn()),
          historyTrimmed: signal(new Set<string>()),
          ...jobs,
        },
      },
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

  it('a reattached panel shows every session so far, including failures the first panel saw (#2472)', async () => {
    // The real registry, the real runner, and a controllable SSE stream —
    // no fake subscription that assumes replay. On master the second
    // panel's listener joins the tab's already-open stream, which only
    // delivers from then on: day 1 and its failure vanish.
    const stub = installEventSourceStub();
    TestBed.configureTestingModule({
      providers: [provideHttpClient(), provideHttpClientTesting(), BackfillJobRunner, DataLakeBackfillStore],
    });
    try {
      // Injecting the runner constructs the root JobsService, whose boot
      // read of active jobs is the first HTTP request to satisfy.
      TestBed.inject(BackfillJobRunner);
      const httpMock = TestBed.inject(HttpTestingController);
      httpMock.expectOne('/api/jobs?active=true').flush([]);

      const dayFrame = (dayIndex: number, failures: unknown[] = []) => ({
        type: 'data_lake.backfill_day',
        trading_date_ms: MAY_20_OPEN_MS + dayIndex * 86_400_000,
        day_index: dayIndex,
        total_days: 3,
        days_remaining: 3 - dayIndex,
        fetched_count: 1,
        reused_count: 0,
        failures,
      });
      const dayOneFailure = [
        {
          artifact_kind: 'minute_trade',
          symbol: 'SPY',
          trading_date_ms: MAY_20_OPEN_MS + 86_400_000,
          data_type: 'trade',
          reason: 'provider_rate_limited',
          detail: '429 from the vendor',
          provider_status_code: 429,
          attempt_count: 3,
        },
      ];

      // Panel one starts the run and watches day 1 fail.
      const first = TestBed.runInInjectionContext(() => new DataLakeBackfillStore());
      const starting = first.start(SPEC);
      httpMock.expectOne(`/api/jobs/${BACKFILL_JOB_TYPE}`).flush({ id: 'job-77', status: 'queued' });
      await starting;
      const stream = stub.last();
      if (!stream) throw new Error('the run opened no event stream');
      stream.dispatch({ type: 'job.started' });
      stream.dispatch(dayFrame(1, dayOneFailure));
      expect(first.days()).toHaveLength(1);
      expect(first.failures()).toHaveLength(1);

      // The operator leaves; the panel is destroyed. The job keeps running
      // against the registry's one stream, which retains what it delivers.
      first.reset();
      stream.dispatch(dayFrame(2));

      // The operator returns: a second panel reattaches mid-flight.
      const second = TestBed.runInInjectionContext(() => new DataLakeBackfillStore());
      second.reattach('job-77');

      expect(second.reattached()).toBe(true);
      expect(second.historyComplete()).toBe(true);
      expect(second.days()).toHaveLength(2);
      expect(second.failures()).toHaveLength(1);
      expect(second.failures()[0]?.reason).toBe('provider_rate_limited');

      // The run continues live and completes; nothing is counted twice and
      // the failure summary matches what the run itself reported.
      stream.dispatch(dayFrame(3));
      stream.dispatch({ type: 'job.completed' });

      expect(second.phase()).toBe('completed');
      expect(second.days()).toHaveLength(3);
      expect(second.failures()).toHaveLength(1);
      expect(second.fetchedCount()).toBe(3);
      httpMock.verify();
    } finally {
      stub.restore();
    }
  });

  it('says its history is partial once the registry has trimmed it, instead of claiming a replay', () => {
    const store = makeStore({ historyTrimmed: signal(new Set(['job-live'])) } as unknown as Partial<JobsService>);

    store.reattach('job-live');

    expect(store.reattached()).toBe(true);
    expect(store.historyComplete()).toBe(false);
  });

  it('reaches its terminal phase after adopting, so the caller can re-read', () => {
    let handler: ((event: { type: string } & Record<string, unknown>) => void) | undefined;
    const onEvent = vi.fn((_jobId: string, h: typeof handler) => {
      handler = h;
      return vi.fn();
    });
    const store = makeStore({ onEvent } as unknown as Partial<JobsService>);
    store.reattach('job-live');

    handler?.({ type: 'job.completed' });

    expect(store.phase()).toBe('completed');
  });

  it('ignores a re-adopt of the run it is already following', () => {
    let handler: ((event: { type: string } & Record<string, unknown>) => void) | undefined;
    const onEvent = vi.fn((_jobId: string, h: typeof handler) => {
      handler = h;
      return vi.fn();
    });
    const store = makeStore({ onEvent } as unknown as Partial<JobsService>);
    store.reattach('job-live');
    handler?.({ type: 'job.progress', current: 1, total: 2, unit: 'days' });

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

  it('names an untyped refusal as a submission failure rather than inventing a reason', async () => {
    const startJob = vi
      .fn()
      .mockRejectedValue(new HttpErrorResponse({ status: 404, statusText: 'Not Found' }));
    const store = makeStore({ startJob } as unknown as Partial<JobsService>);

    await store.start(SPEC);

    expect(store.phase()).toBe('failed');
    expect(store.error()?.code).toBe('submission_failed');
  });

  it("carries a typed rejection's own reason code onto the run", async () => {
    const startJob = vi.fn().mockRejectedValue(
      new HttpErrorResponse({
        status: 422,
        statusText: 'Unprocessable Entity',
        error: {
          detail: { reason: 'range_too_large', message: 'range is 3654 days' },
        },
      }),
    );
    const store = makeStore({ startJob } as unknown as Partial<JobsService>);

    await store.start(SPEC);

    expect(store.error()).toEqual({
      code: 'range_too_large',
      message: 'range is 3654 days',
    });
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
    let handler: ((event: { type: string } & Record<string, unknown>) => void) | undefined;
    const onEvent = vi.fn((_jobId: string, h: typeof handler) => {
      handler = h;
      return vi.fn();
    });
    const store = makeStore({ onEvent } as unknown as Partial<JobsService>);
    store.reattach('job-live');

    handler?.({ type: 'job.progress', current: 3, total: 5, unit: 'days' });
    handler?.({ type: 'job.completed' });

    expect(store.progress()).toMatchObject({
      current: 3,
      total: 5,
      unit: 'days',
    });
    expect(store.phase()).toBe('completed');
    expect(store.running()).toBe(false);
  });

  it('keeps the failure code the job reported', () => {
    let handler: ((event: { type: string } & Record<string, unknown>) => void) | undefined;
    const onEvent = vi.fn((_jobId: string, h: typeof handler) => {
      handler = h;
      return vi.fn();
    });
    const store = makeStore({ onEvent } as unknown as Partial<JobsService>);
    store.reattach('job-live');

    handler?.({ type: 'job.failed', code: 'PythonRejected', message: 'boom' });

    expect(store.error()).toEqual({ code: 'PythonRejected', message: 'boom' });
    expect(store.phase()).toBe('failed');
  });

  it('ignores an event type it does not know', () => {
    const store = makeStore({} as Partial<JobsService>);

    store.ingestEvent({ type: 'something.else' });

    expect(store.phase()).toBe('idle');
  });

  it('a frame delivered through the registered handler folds the same as a direct ingestEvent() call', async () => {
    let handler: ((event: { type: string } & Record<string, unknown>) => void) | undefined;
    const startJob = vi.fn().mockResolvedValue('job-1');
    const onEvent = vi.fn((_jobId: string, h: typeof handler) => {
      handler = h;
      return vi.fn();
    });
    const store = makeStore({
      startJob,
      onEvent,
    } as unknown as Partial<JobsService>);

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
    const store = makeStore({
      startJob,
      onEvent,
    } as unknown as Partial<JobsService>);

    await store.start(SPEC);
    handler?.({ type: 'job.completed' });

    expect(unsubscribe).toHaveBeenCalledTimes(1);
  });
});

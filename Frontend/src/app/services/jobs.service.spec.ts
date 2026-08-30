import { TestBed } from '@angular/core/testing';
import { provideHttpClient } from '@angular/common/http';
import {
  HttpTestingController,
  provideHttpClientTesting,
} from '@angular/common/http/testing';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { JobsService, applyJobEvent, streamTimestamp, type JobState } from './jobs.service';

function queuedJob(): JobState {
  return {
    id: 'job-1',
    type: 'engine_backtest',
    status: 'queued',
    recentLogs: [],
    logSeq: 0,
  };
}

describe('JobsService SSE reducer', () => {
  it('uses the Redis stream id as the authoritative server event timestamp', () => {
    const started = applyJobEvent(
      queuedJob(),
      { type: 'job.started' },
      '1783896000123-0',
    );

    expect(started.startedAt).toBe(1_783_896_000_123);
    expect(started.recentEvents?.[0]).toMatchObject({
      id: '1783896000123-0',
      timestamp: 1_783_896_000_123,
      type: 'job.started',
      summary: 'Run started',
    });
  });

  it('records phase and progress events in the structured timeline', () => {
    const phased = applyJobEvent(
      queuedJob(),
      { type: 'job.phase', phase: 'running_indicators', friendly: 'Running indicators' },
      '1783896001000-0',
    );
    const progressed = applyJobEvent(
      phased,
      { type: 'job.progress', current: 250, total: 1000, unit: 'bars', message: 'Evaluated' },
      '1783896002000-0',
    );

    expect(progressed.recentEvents?.map((event) => event.summary)).toEqual([
      'Running indicators',
      'Evaluated · 250 / 1,000 bars',
    ]);
  });

  it('deduplicates a replayed SSE event by stream id', () => {
    const event = { type: 'job.phase' as const, phase: 'persisting' };
    const once = applyJobEvent(queuedJob(), event, '1783896003000-0');
    const replayed = applyJobEvent(once, event, '1783896003000-0');

    expect(replayed.recentEvents).toHaveLength(1);
  });

  it('rejects malformed stream ids as timestamps', () => {
    expect(streamTimestamp('not-a-stream-id')).toBeNull();
    expect(streamTimestamp('0-0')).toBeNull();
  });
});

// ── JobsService.onEvent (#1856) ───────────────────────────────────────
//
// Give JobsService a per-job event hook so domain-specific consumers
// (RunSessionService, DataLakeBackfillStore) ride its one EventSource
// instead of each opening a second one to the same endpoint. These tests
// exercise the real service against a controllable EventSource stub —
// jsdom has none — so a genuine SSE frame drives the hook end to end,
// including a domain event type outside JobEvent's own closed union.

interface ControllableEventSource {
  onmessage: ((ev: { data: string; lastEventId?: string }) => void) | null;
  onerror: (() => void) | null;
  close: () => void;
  dispatch: (payload: Record<string, unknown>, lastEventId?: string) => void;
}

describe('JobsService.onEvent', () => {
  let service: JobsService;
  let httpMock: HttpTestingController;
  let originalEventSource: typeof EventSource;
  let lastSource: ControllableEventSource | null;

  beforeEach(() => {
    lastSource = null;
    originalEventSource = globalThis.EventSource;
    const setLastSource = (instance: ControllableEventSource): void => {
      lastSource = instance;
    };
    class StubEventSource implements ControllableEventSource {
      onmessage: ((ev: { data: string; lastEventId?: string }) => void) | null = null;
      onerror: (() => void) | null = null;
      constructor() {
        // Storing the reference via a setter keeps the lint rule against
        // `this` aliasing happy without disabling it.
        setLastSource(this);
      }
      close(): void {
        // No-op: the service's close() call flips its own bookkeeping.
      }
      dispatch(payload: Record<string, unknown>, lastEventId = ''): void {
        this.onmessage?.({ data: JSON.stringify(payload), lastEventId });
      }
    }
    (globalThis as unknown as { EventSource: typeof EventSource }).EventSource =
      StubEventSource as unknown as typeof EventSource;

    TestBed.configureTestingModule({
      providers: [provideHttpClient(), provideHttpClientTesting()],
    });
    service = TestBed.inject(JobsService);
    httpMock = TestBed.inject(HttpTestingController);
    // The constructor fires resumeActive(); this test suite always starts
    // from an empty active-job list.
    httpMock.expectOne((r) => r.url === '/api/jobs').flush([]);
  });

  afterEach(() => {
    (globalThis as unknown as { EventSource: typeof EventSource }).EventSource = originalEventSource;
    httpMock.verify();
    vi.restoreAllMocks();
  });

  async function startJobAndGrabSource(): Promise<ControllableEventSource> {
    const startPromise = service.startJob('dataset-zip', { ticker: 'SPY' });
    httpMock.expectOne('/api/jobs/dataset-zip').flush({ id: 'job-1', status: 'queued' });
    await startPromise;
    if (!lastSource) throw new Error('EventSource stub was not constructed');
    return lastSource;
  }

  it('delivers a raw frame to a registered handler, including a domain type outside JobEventType', async () => {
    const source = await startJobAndGrabSource();
    const received: unknown[] = [];

    service.onEvent('job-1', (event) => received.push(event));
    source.dispatch({ type: 'chunk_plan', total: 3 });

    expect(received).toEqual([{ type: 'chunk_plan', total: 3 }]);
  });

  it('still folds the same frame into JobState — the hook does not replace the existing reducer', async () => {
    const source = await startJobAndGrabSource();

    service.onEvent('job-1', () => {});
    source.dispatch({ type: 'job.phase', phase: 'running_indicators' });

    expect(service.job('job-1')?.phase).toBe('running_indicators');
  });

  it('delivers one frame to every registered handler on the same job', async () => {
    const source = await startJobAndGrabSource();
    const a: unknown[] = [];
    const b: unknown[] = [];

    service.onEvent('job-1', (event) => a.push(event));
    service.onEvent('job-1', (event) => b.push(event));
    source.dispatch({ type: 'job.started' });

    expect(a).toHaveLength(1);
    expect(b).toHaveLength(1);
  });

  it('stops delivering once the returned unsubscribe function is called', async () => {
    const source = await startJobAndGrabSource();
    const received: unknown[] = [];

    const unsubscribe = service.onEvent('job-1', (event) => received.push(event));
    source.dispatch({ type: 'job.started' });
    unsubscribe();
    source.dispatch({ type: 'job.phase', phase: 'running_indicators' });

    expect(received).toHaveLength(1);
  });

  it('never delivers to a handler registered on a different job id', async () => {
    const source = await startJobAndGrabSource();
    const received: unknown[] = [];

    service.onEvent('some-other-job', (event) => received.push(event));
    source.dispatch({ type: 'job.started' });

    expect(received).toHaveLength(0);
  });

  it('clears a listener that never unsubscribed once the job reaches a terminal event', async () => {
    // Backstop cleanup: closeStream() (fired internally once a terminal
    // event lands) clears this job's listener registrations too, so a
    // caller that forgot to call its own unsubscribe function doesn't leak
    // one forever. Dispatched via the stub directly rather than through
    // .close(), which is a no-op here — this isolates the assertion to
    // whether the listener map was cleared, not whether the transport
    // stopped delivering messages on its own.
    const source = await startJobAndGrabSource();
    const received: unknown[] = [];
    service.onEvent('job-1', (event) => received.push(event));

    source.dispatch({ type: 'job.completed' });
    expect(received).toHaveLength(1);
    // closeStream() is deferred via setTimeout(…, 0) so the terminal
    // event's own onEvent delivery isn't cut off mid-dispatch.
    await new Promise((resolve) => setTimeout(resolve, 0));

    source.dispatch({ type: 'job.log', message: 'after terminal' });
    expect(received).toHaveLength(1);
  });
});

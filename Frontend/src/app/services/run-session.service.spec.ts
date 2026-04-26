/**
 * RunSessionService — drives the run-card state machine off SSE events
 * routed through the unified JobsService.
 *
 * These tests stub two things:
 *   1. ``JobsService`` is replaced with a fake whose ``startJob`` returns
 *      a synthetic id and whose ``cancelJob`` is observable. We don't
 *      exercise the real Redis-backed transport.
 *   2. The global ``EventSource`` constructor is replaced with a
 *      controllable test double so tests can inject domain events
 *      (chunk_plan, bundle_progress, job.completed, …) verbatim.
 *
 * The download path (``/api/jobs/{id}/download``) goes through ``fetch``
 * which we stub on a per-test basis when the happy-path branch is
 * exercised.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { TestBed } from '@angular/core/testing';
import { JobsService } from './jobs.service';
import { RunSessionService } from './run-session.service';

// ── Test doubles ────────────────────────────────────────────────────

interface ControllableEventSource {
  url: string;
  onmessage: ((ev: { data: string }) => void) | null;
  onerror: ((ev: unknown) => void) | null;
  close: () => void;
  dispatch: (payload: Record<string, unknown>) => void;
}

let lastSource: ControllableEventSource | null = null;
let originalEventSource: typeof EventSource;

function setLastSource(instance: ControllableEventSource): void {
  lastSource = instance;
}

function installEventSourceStub(): void {
  originalEventSource = globalThis.EventSource;
  class StubEventSource implements ControllableEventSource {
    url: string;
    onmessage: ((ev: { data: string }) => void) | null = null;
    onerror: ((ev: unknown) => void) | null = null;
    constructor(url: string) {
      this.url = url;
      // The test harness needs a reference to the latest instance so it
      // can dispatch events; storing it via a setter keeps the lint rule
      // against ``this`` aliasing happy without disabling it.
      setLastSource(this);
    }
    close(): void {
      // No-op for the stub; the service's call to close() flips a flag.
    }
    dispatch(payload: Record<string, unknown>): void {
      this.onmessage?.({ data: JSON.stringify(payload) });
    }
  }
  // EventSource is typed strictly; cast through unknown for the stub.
  (globalThis as unknown as { EventSource: typeof EventSource }).EventSource =
    StubEventSource as unknown as typeof EventSource;
}

function restoreEventSource(): void {
  (globalThis as unknown as { EventSource: typeof EventSource }).EventSource = originalEventSource;
  lastSource = null;
}

// Mocked JobsService — only the methods RunSessionService actually calls.
function mockJobsService(idToReturn = 'job-test-id') {
  return {
    startJob: vi.fn().mockResolvedValue(idToReturn),
    cancelJob: vi.fn().mockResolvedValue(undefined),
    // The real service exposes more, but RunSessionService only calls these two.
  };
}

/**
 * Drive `service.start()` until the EventSource handle is wired up,
 * then return the stub so the caller can dispatch events. ``start()``
 * resolves only after a terminal event closes the stream, so we run it
 * unawaited and let the caller resolve it by dispatching ``job.completed``
 * (or failed/cancelled) themselves.
 */
async function startAndGrabSource(
  service: RunSessionService,
  payload: Record<string, unknown> = { ticker: 'SPY' },
  options?: { downloadOnComplete?: boolean },
): Promise<{ done: Promise<void>; source: ControllableEventSource }> {
  const done = service.start(payload, options);
  // Let the microtask that opens the EventSource run.
  await Promise.resolve();
  await Promise.resolve();
  if (!lastSource) throw new Error('EventSource stub was not constructed');
  return { done, source: lastSource };
}

// ── Tests ───────────────────────────────────────────────────────────

describe('RunSessionService', () => {
  let service: RunSessionService;
  let jobsMock: ReturnType<typeof mockJobsService>;

  beforeEach(() => {
    installEventSourceStub();
    jobsMock = mockJobsService('sess-1');
    TestBed.configureTestingModule({
      providers: [{ provide: JobsService, useValue: jobsMock }],
    });
    service = TestBed.inject(RunSessionService);
  });

  afterEach(() => {
    restoreEventSource();
    vi.restoreAllMocks();
  });

  it('starts in idle state with no chunks or result', () => {
    expect(service.state()).toBe('idle');
    expect(service.chunks()).toEqual([]);
    expect(service.result()).toBeNull();
    expect(service.error()).toBeNull();
  });

  it('walks fetching → bundling → done on a happy-path stream', async () => {
    // Stub fetch for the auto-download leg only.
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      new Response(new Blob(['x']), { status: 200 }),
    );
    vi.spyOn(URL, 'createObjectURL').mockReturnValue('blob:fake');
    vi.spyOn(URL, 'revokeObjectURL').mockImplementation(() => undefined);
    const click = vi.fn();
    vi.spyOn(document, 'createElement').mockImplementation(((tag: string) => {
      if (tag === 'a') return { href: '', download: '', click } as unknown as HTMLAnchorElement;
      return document.createElement(tag);
    }) as typeof document.createElement);

    const { done, source } = await startAndGrabSource(service);

    source.dispatch({ type: 'job.started' });
    source.dispatch({ type: 'chunk_plan', total: 2 });
    source.dispatch({ type: 'chunk_start', index: 1, total: 2, from: '2026-01-01', to: '2026-02-01' });
    source.dispatch({ type: 'chunk_done', index: 1, total: 2, bars_returned: 5000 });
    source.dispatch({ type: 'chunk_start', index: 2, total: 2, from: '2026-02-02', to: '2026-03-01' });
    source.dispatch({ type: 'chunk_done', index: 2, total: 2, bars_returned: 3000 });
    source.dispatch({ type: 'fetch_complete', raw_bars: 8000, processed_bars: 7800, indicator_columns: 5 });
    source.dispatch({ type: 'job.phase', phase: 'bundling' });
    source.dispatch({ type: 'bundle_start', components: ['dataset.csv', 'metadata.csv'] });
    source.dispatch({ type: 'bundle_component_done', name: 'dataset.csv' });
    source.dispatch({ type: 'bundle_component_done', name: 'metadata.csv' });
    source.dispatch({
      type: 'job.completed',
      download_url: '/api/jobs/sess-1/download',
      filename: 'SPY.zip',
      size_bytes: 4096,
    });

    await done;

    expect(jobsMock.startJob).toHaveBeenCalledWith('dataset-zip', { dataset: { ticker: 'SPY' } });
    expect(service.state()).toBe('done');
    expect(service.sessionId()).toBe('sess-1');
    expect(service.result()).toEqual({
      sessionId: 'sess-1',
      filename: 'SPY.zip',
      sizeBytes: 4096,
      downloadUrl: '/api/jobs/sess-1/download',
    });
    expect(service.chunks()).toHaveLength(2);
    expect(service.chunks().every((c) => c.status === 'done')).toBe(true);
    expect(service.bundleComponents().every((c) => c.status === 'done')).toBe(true);
    expect(click).toHaveBeenCalled();
  });

  it('marks the next-up queued chunk as paced when chunk_paced fires', async () => {
    const { done, source } = await startAndGrabSource(service, { ticker: 'SPY' }, { downloadOnComplete: false });

    source.dispatch({ type: 'job.started' });
    source.dispatch({ type: 'chunk_plan', total: 3 });
    source.dispatch({ type: 'chunk_start', index: 1, total: 3, from: 'a', to: 'b' });
    source.dispatch({ type: 'chunk_done', index: 1, total: 3, bars_returned: 100 });
    source.dispatch({ type: 'chunk_paced', wait_seconds: 9.4, label: 'aggs:SPY' });

    const queued = service.chunks().find((c) => c.status === 'queued');
    expect(queued).toBeDefined();
    expect(queued!.waitSeconds).toBe(9);

    // Close the stream so the start() promise resolves and Vitest doesn't
    // hang waiting for it.
    source.dispatch({ type: 'job.cancelled', reason: 'test cleanup' });
    await done;
  });

  it('transitions to error on a job.failed event', async () => {
    const { done, source } = await startAndGrabSource(service, { ticker: 'XYZ' }, { downloadOnComplete: false });

    source.dispatch({ type: 'job.started' });
    source.dispatch({ type: 'job.failed', code: 'HTTPException', message: 'No bars returned' });

    await done;

    expect(service.state()).toBe('error');
    expect(service.error()).toEqual({ kind: 'internal', message: 'No bars returned' });
  });

  it('reset() returns the state machine to idle', async () => {
    const { done, source } = await startAndGrabSource(service, { ticker: 'SPY' }, { downloadOnComplete: false });
    source.dispatch({ type: 'job.started' });
    source.dispatch({ type: 'job.cancelled', reason: 'manual abort' });
    await done;
    expect(service.state()).toBe('error');

    service.reset();

    expect(service.state()).toBe('idle');
    expect(service.error()).toBeNull();
    expect(service.chunks()).toEqual([]);
  });

  it('progressFraction reflects done-chunks during fetching', async () => {
    const { done, source } = await startAndGrabSource(service, { ticker: 'SPY' }, { downloadOnComplete: false });

    source.dispatch({ type: 'job.started' });
    source.dispatch({ type: 'chunk_plan', total: 4 });
    source.dispatch({ type: 'chunk_start', index: 1, total: 4, from: 'a', to: 'b' });
    source.dispatch({ type: 'chunk_done', index: 1, total: 4, bars_returned: 1 });
    source.dispatch({ type: 'chunk_start', index: 2, total: 4, from: 'b', to: 'c' });
    source.dispatch({ type: 'chunk_done', index: 2, total: 4, bars_returned: 1 });

    expect(service.progressFraction()).toBeCloseTo(0.5);

    source.dispatch({ type: 'job.cancelled', reason: 'test cleanup' });
    await done;
  });

  it('bundle_progress populates bundleProgress and clears it when its parent component finishes', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      new Response(new Blob(['x']), { status: 200 }),
    );
    vi.spyOn(URL, 'createObjectURL').mockReturnValue('blob:fake');
    vi.spyOn(URL, 'revokeObjectURL').mockImplementation(() => undefined);
    vi.spyOn(document, 'createElement').mockImplementation(((tag: string) => {
      if (tag === 'a') return { href: '', download: '', click: vi.fn() } as unknown as HTMLAnchorElement;
      return document.createElement(tag);
    }) as typeof document.createElement);

    const { done, source } = await startAndGrabSource(service);

    source.dispatch({ type: 'job.started' });
    source.dispatch({ type: 'chunk_plan', total: 1 });
    source.dispatch({ type: 'chunk_start', index: 1, total: 1, from: 'a', to: 'b' });
    source.dispatch({ type: 'chunk_done', index: 1, total: 1, bars_returned: 100 });
    source.dispatch({ type: 'fetch_complete' });
    source.dispatch({ type: 'job.phase', phase: 'bundling' });
    source.dispatch({ type: 'bundle_start', components: ['options_calls.csv', 'metadata.csv'] });
    source.dispatch({
      type: 'bundle_progress',
      component: 'options_calls.csv',
      step: 47,
      label: 'O:SPY260417C00705000',
    });
    source.dispatch({ type: 'bundle_component_done', name: 'options_calls.csv' });
    source.dispatch({ type: 'bundle_component_done', name: 'metadata.csv' });
    source.dispatch({
      type: 'job.completed',
      download_url: '/api/jobs/sess-1/download',
      filename: 'SPY.zip',
      size_bytes: 2048,
    });

    await done;

    expect(service.bundleProgress()).toBeNull();
    expect(service.bundleComponents().every((c) => c.status === 'done')).toBe(true);
    expect(service.state()).toBe('done');
  });

  it('bundle_progress for one component does not clear when a different component finishes', async () => {
    const { done, source } = await startAndGrabSource(service, { ticker: 'SPY' }, { downloadOnComplete: false });

    source.dispatch({ type: 'job.started' });
    source.dispatch({ type: 'chunk_plan', total: 1 });
    source.dispatch({ type: 'chunk_start', index: 1, total: 1, from: 'a', to: 'b' });
    source.dispatch({ type: 'chunk_done', index: 1, total: 1, bars_returned: 1 });
    source.dispatch({ type: 'fetch_complete' });
    source.dispatch({ type: 'bundle_start', components: ['options_calls.csv', 'metadata.csv'] });
    source.dispatch({
      type: 'bundle_progress',
      component: 'options_calls.csv',
      step: 12,
    });
    source.dispatch({ type: 'bundle_component_done', name: 'metadata.csv' });

    const progress = service.bundleProgress();
    expect(progress).not.toBeNull();
    expect(progress!.component).toBe('options_calls.csv');
    expect(progress!.step).toBe(12);

    source.dispatch({ type: 'job.cancelled', reason: 'test cleanup' });
    await done;
  });

  it('bundle_component_start flips a component to fetching and bundle_component_done flips it to done', async () => {
    const { done, source } = await startAndGrabSource(service, { ticker: 'SPY' }, { downloadOnComplete: false });

    source.dispatch({ type: 'job.started' });
    source.dispatch({ type: 'fetch_complete' });
    source.dispatch({ type: 'bundle_start', components: ['news.csv', 'financials.csv'] });

    expect(service.bundleComponents().every((c) => c.status === 'queued')).toBe(true);

    source.dispatch({ type: 'bundle_component_start', name: 'news.csv' });
    expect(service.bundleComponents().find((c) => c.name === 'news.csv')!.status).toBe('fetching');
    expect(service.bundleComponents().find((c) => c.name === 'financials.csv')!.status).toBe('queued');

    source.dispatch({ type: 'bundle_component_done', name: 'news.csv' });
    expect(service.bundleComponents().find((c) => c.name === 'news.csv')!.status).toBe('done');

    source.dispatch({ type: 'job.cancelled', reason: 'test cleanup' });
    await done;
  });

  it('processing_indicators populates the indicator-phase signal and bundle_start clears it', async () => {
    const { done, source } = await startAndGrabSource(service, { ticker: 'SPY' }, { downloadOnComplete: false });

    source.dispatch({ type: 'job.started' });
    source.dispatch({ type: 'chunk_plan', total: 1 });
    source.dispatch({ type: 'chunk_done', index: 1, total: 1, bars_returned: 100 });
    expect(service.processingIndicators()).toBeNull();

    source.dispatch({ type: 'processing_indicators', indicator_count: 7, bar_count: 8000 });
    expect(service.processingIndicators()).toEqual({ indicatorCount: 7, barCount: 8000 });

    source.dispatch({ type: 'bundle_start', components: ['dataset.csv'] });
    expect(service.processingIndicators()).toBeNull();

    source.dispatch({ type: 'job.cancelled', reason: 'test cleanup' });
    await done;
  });

  it('cancel() routes through JobsService.cancelJob', async () => {
    const { done, source } = await startAndGrabSource(service, { ticker: 'SPY' }, { downloadOnComplete: false });
    source.dispatch({ type: 'job.started' });

    await service.cancel();

    expect(jobsMock.cancelJob).toHaveBeenCalledWith('sess-1');

    // Close the stream cleanly.
    source.dispatch({ type: 'job.cancelled', reason: 'manual' });
    await done;
  });
});

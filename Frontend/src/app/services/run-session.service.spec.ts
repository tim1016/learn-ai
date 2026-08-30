/**
 * RunSessionService — drives the run-card state machine off SSE events
 * routed through the unified JobsService.
 *
 * ``JobsService`` is replaced with a fake whose ``startJob`` returns a
 * synthetic id, ``cancelJob`` is observable, and ``onEvent`` records the
 * handler RunSessionService registers per job (#1856 — the real service
 * multiplexes one EventSource across every domain consumer; this fake
 * exposes that same seam so tests dispatch events by calling the handler
 * directly instead of standing up a transport double).
 *
 * The download path (``/api/jobs/{id}/download``) goes through ``fetch``
 * which we stub on a per-test basis when the happy-path branch is
 * exercised.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { TestBed } from '@angular/core/testing';
import { JobsService, type JobStreamEvent } from './jobs.service';
import { RunSessionService } from './run-session.service';

// ── Test doubles ────────────────────────────────────────────────────

/** Mocked JobsService — only the methods RunSessionService actually calls. */
function mockJobsService(idToReturn = 'job-test-id') {
  const handlersByJob = new Map<string, (event: JobStreamEvent) => void>();
  return {
    startJob: vi.fn().mockResolvedValue(idToReturn),
    cancelJob: vi.fn().mockResolvedValue(undefined),
    onEvent: vi.fn((jobId: string, handler: (event: JobStreamEvent) => void) => {
      handlersByJob.set(jobId, handler);
      return vi.fn(() => handlersByJob.delete(jobId));
    }),
    // Test-only helper — not part of the real JobsService surface.
    dispatch(jobId: string, payload: Record<string, unknown>): void {
      handlersByJob.get(jobId)?.(payload as JobStreamEvent);
    },
  };
}

/**
 * Drive `service.start()` until it has registered its `onEvent` handler,
 * then return a `dispatch` bound to that job so the caller can inject
 * events. ``start()`` resolves only after a terminal event closes the
 * stream, so we run it unawaited and let the caller resolve it by
 * dispatching ``job.completed`` (or failed/cancelled) themselves.
 */
async function startAndGrabSource(
  service: RunSessionService,
  jobsMock: ReturnType<typeof mockJobsService>,
  payload: Record<string, unknown> = { ticker: 'SPY' },
  options?: { downloadOnComplete?: boolean },
): Promise<{ done: Promise<void>; source: { dispatch: (payload: Record<string, unknown>) => void } }> {
  const done = service.start(payload, options);
  // Let the microtask that calls onEvent() run.
  await Promise.resolve();
  await Promise.resolve();
  const jobId = await jobsMock.startJob.mock.results[0]?.value;
  return { done, source: { dispatch: (p) => jobsMock.dispatch(jobId, p) } };
}

// ── Tests ───────────────────────────────────────────────────────────

describe('RunSessionService', () => {
  let service: RunSessionService;
  let jobsMock: ReturnType<typeof mockJobsService>;

  beforeEach(() => {
    jobsMock = mockJobsService('sess-1');
    TestBed.configureTestingModule({
      providers: [{ provide: JobsService, useValue: jobsMock }],
    });
    service = TestBed.inject(RunSessionService);
  });

  afterEach(() => {
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

    const { done, source } = await startAndGrabSource(service, jobsMock);

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
    const { done, source } = await startAndGrabSource(service, jobsMock, { ticker: 'SPY' }, { downloadOnComplete: false });

    source.dispatch({ type: 'job.started' });
    source.dispatch({ type: 'chunk_plan', total: 3 });
    source.dispatch({ type: 'chunk_start', index: 1, total: 3, from: 'a', to: 'b' });
    source.dispatch({ type: 'chunk_done', index: 1, total: 3, bars_returned: 100 });
    source.dispatch({ type: 'chunk_paced', wait_seconds: 9.4, label: 'aggs:SPY' });

    const queued = service.chunks().find((c) => c.status === 'queued');
    expect(queued).toBeDefined();
    if (!queued) throw new Error('Expected a queued chunk');
    expect(queued.waitSeconds).toBe(9);

    // Close the stream so the start() promise resolves and Vitest doesn't
    // hang waiting for it.
    source.dispatch({ type: 'job.cancelled', reason: 'test cleanup' });
    await done;
  });

  it('transitions to error on a job.failed event', async () => {
    const { done, source } = await startAndGrabSource(service, jobsMock, { ticker: 'XYZ' }, { downloadOnComplete: false });

    source.dispatch({ type: 'job.started' });
    source.dispatch({ type: 'job.failed', code: 'HTTPException', message: 'No bars returned' });

    await done;

    expect(service.state()).toBe('error');
    expect(service.error()).toEqual({ kind: 'internal', message: 'No bars returned' });
  });

  it('reset() returns the state machine to idle', async () => {
    const { done, source } = await startAndGrabSource(service, jobsMock, { ticker: 'SPY' }, { downloadOnComplete: false });
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
    const { done, source } = await startAndGrabSource(service, jobsMock, { ticker: 'SPY' }, { downloadOnComplete: false });

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

    const { done, source } = await startAndGrabSource(service, jobsMock);

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
    const { done, source } = await startAndGrabSource(service, jobsMock, { ticker: 'SPY' }, { downloadOnComplete: false });

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
    if (!progress) throw new Error('Expected bundle progress');
    expect(progress.component).toBe('options_calls.csv');
    expect(progress.step).toBe(12);

    source.dispatch({ type: 'job.cancelled', reason: 'test cleanup' });
    await done;
  });

  it('bundle_component_start flips a component to fetching and bundle_component_done flips it to done', async () => {
    const { done, source } = await startAndGrabSource(service, jobsMock, { ticker: 'SPY' }, { downloadOnComplete: false });

    source.dispatch({ type: 'job.started' });
    source.dispatch({ type: 'fetch_complete' });
    source.dispatch({ type: 'bundle_start', components: ['news.csv', 'financials.csv'] });

    expect(service.bundleComponents().every((c) => c.status === 'queued')).toBe(true);

    source.dispatch({ type: 'bundle_component_start', name: 'news.csv' });
    const news = service.bundleComponents().find((c) => c.name === 'news.csv');
    const financials = service.bundleComponents().find((c) => c.name === 'financials.csv');
    if (!news || !financials) throw new Error('Expected both bundle components');
    expect(news.status).toBe('fetching');
    expect(financials.status).toBe('queued');

    source.dispatch({ type: 'bundle_component_done', name: 'news.csv' });
    const completedNews = service.bundleComponents().find((c) => c.name === 'news.csv');
    if (!completedNews) throw new Error('Expected the news bundle component');
    expect(completedNews.status).toBe('done');

    source.dispatch({ type: 'job.cancelled', reason: 'test cleanup' });
    await done;
  });

  it('processing_indicators populates the indicator-phase signal and bundle_start clears it', async () => {
    const { done, source } = await startAndGrabSource(service, jobsMock, { ticker: 'SPY' }, { downloadOnComplete: false });

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

  it('cancel() routes through JobsService.cancelJob without closing the stream itself', async () => {
    const { done, source } = await startAndGrabSource(service, jobsMock, { ticker: 'SPY' }, { downloadOnComplete: false });
    source.dispatch({ type: 'job.started' });
    const unsubscribe = await jobsMock.onEvent.mock.results[0]?.value;

    await service.cancel();

    expect(jobsMock.cancelJob).toHaveBeenCalledWith('sess-1');
    // cancel() must not unsubscribe on its own — only the worker's own
    // eventual terminal event (job.cancelled here) does that, via the same
    // path any other terminal event takes. Closing early here would mean
    // that event, and the state transition it drives, could never arrive.
    expect(unsubscribe).not.toHaveBeenCalled();

    source.dispatch({ type: 'job.cancelled', reason: 'manual' });
    await done;

    expect(service.state()).toBe('error');
    expect(service.error()).toEqual({ kind: 'cancelled', message: 'manual' });
    expect(unsubscribe).toHaveBeenCalledTimes(1);
  });

  // ── Run-dock event log ───────────────────────────────────────────

  it('appends a log entry for each meaningful SSE event with the right severity', async () => {
    const { done, source } = await startAndGrabSource(
      service,
      jobsMock,
      { ticker: 'SPY', from_date: '2025-01-06', to_date: '2025-01-10' },
      { downloadOnComplete: false },
    );
    // start() seeded "starting run · …" + "job id …"; dispatch from there.
    source.dispatch({ type: 'job.started' });
    source.dispatch({ type: 'chunk_plan', total: 2 });
    source.dispatch({ type: 'chunk_done', index: 1, total: 2, bars_returned: 1234 });
    source.dispatch({ type: 'chunk_paced', wait_seconds: 9.4 });
    source.dispatch({ type: 'fetch_complete', raw_bars: 8000, processed_bars: 7800, indicator_columns: 25 });
    source.dispatch({ type: 'job.failed', message: 'boom' });

    const log = service.log();
    const levels = log.map((e) => e.level);
    // Severity mapping is the contract — confirm each landed.
    expect(levels).toContain('warn'); // chunk_paced
    expect(levels).toContain('success'); // chunk_done + fetch_complete
    expect(levels).toContain('error'); // job.failed
    expect(levels).toContain('info'); // job.started + chunk_plan + others

    const failed = log.find((e) => e.message.startsWith('failed:'));
    expect(failed?.level).toBe('error');
    expect(failed?.glyph).toBe('✗');

    const paced = log.find((e) => e.message.includes('pacing'));
    expect(paced?.level).toBe('warn');

    const fetchDone = log.find((e) => e.message.startsWith('fetch complete'));
    expect(fetchDone?.level).toBe('success');
    expect(fetchDone?.message).toContain('25 indicator');

    await done;
  });

  it('caps the log at 500 entries, FIFO — oldest entries roll off when over the limit', async () => {
    const { done, source } = await startAndGrabSource(service, jobsMock, { ticker: 'SPY' }, { downloadOnComplete: false });

    // Seed a recognisable first entry, then push enough events to exceed 500.
    source.dispatch({ type: 'chunk_plan', total: 999 });
    for (let i = 1; i <= 600; i++) {
      source.dispatch({ type: 'chunk_done', index: i, total: 999, bars_returned: i });
    }

    const log = service.log();
    expect(log.length).toBe(500);
    // The earliest "chunk_plan" entry plus the first ~100 chunk_done entries
    // should have rolled off; the buffer's tail must contain the latest.
    expect(log[log.length - 1].message).toContain('chunk 600');

    source.dispatch({ type: 'job.cancelled', reason: 'cleanup' });
    await done;
  });

  it('log persists across reset() — the dock keeps history when a new run starts', async () => {
    const { done, source } = await startAndGrabSource(service, jobsMock, { ticker: 'SPY' }, { downloadOnComplete: false });
    source.dispatch({ type: 'chunk_plan', total: 1 });
    source.dispatch({ type: 'job.cancelled', reason: 'first run' });
    await done;

    const lengthBeforeReset = service.log().length;
    expect(lengthBeforeReset).toBeGreaterThan(0);

    service.reset();
    expect(service.log().length).toBe(lengthBeforeReset); // unchanged
    expect(service.state()).toBe('idle');

    // clearLog() is the only path that wipes — confirm it actually does.
    service.clearLog();
    expect(service.log().length).toBe(0);
  });

  it('unknown event types still produce a log line so future events are not silently dropped', async () => {
    const { done, source } = await startAndGrabSource(service, jobsMock, { ticker: 'SPY' }, { downloadOnComplete: false });
    const beforeCount = service.log().length;

    source.dispatch({ type: 'totally_made_up_event', anything: 'goes' });

    const after = service.log();
    expect(after.length).toBe(beforeCount + 1);
    expect(after[after.length - 1].message).toBe('totally_made_up_event');

    source.dispatch({ type: 'job.cancelled', reason: 'cleanup' });
    await done;
  });

  it('dividend_adjusted event lands in the log even though it does not affect state', async () => {
    const { done, source } = await startAndGrabSource(service, jobsMock, { ticker: 'SPY' }, { downloadOnComplete: false });

    source.dispatch({ type: 'dividend_adjusted', events: 4, bars: 1500 });

    const matched = service.log().find((e) => e.message.startsWith('dividend adjustment'));
    expect(matched).toBeDefined();
    expect(matched?.message).toContain('4 events');
    expect(matched?.message).toContain('1,500 bars');

    source.dispatch({ type: 'job.cancelled', reason: 'cleanup' });
    await done;
  });
});

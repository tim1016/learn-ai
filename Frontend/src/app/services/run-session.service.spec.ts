/**
 * RunSessionService — drives the run-card state machine off an SSE stream.
 *
 * These tests stub ``fetch`` to return synthetic SSE responses and
 * verify the service's signals settle into the right state for each
 * event sequence (fetching → bundling → done; cancel; error). We never
 * hit the real Python service.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { TestBed } from '@angular/core/testing';
import { RunSessionService } from './run-session.service';

/**
 * Build a Response-like object whose body is a ReadableStream emitting
 * the given SSE event chunks (already-formatted ``data: …\n\n`` strings).
 */
function sseResponse(chunks: string[]): Response {
  const encoder = new TextEncoder();
  const stream = new ReadableStream<Uint8Array>({
    start(controller) {
      for (const chunk of chunks) {
        controller.enqueue(encoder.encode(chunk));
      }
      controller.close();
    },
  });
  return new Response(stream, {
    status: 200,
    headers: { 'Content-Type': 'text/event-stream' },
  });
}

function event(payload: Record<string, unknown>): string {
  return `data: ${JSON.stringify(payload)}\n\n`;
}

describe('RunSessionService', () => {
  let service: RunSessionService;

  beforeEach(() => {
    TestBed.configureTestingModule({});
    service = TestBed.inject(RunSessionService);
  });

  afterEach(() => {
    // Restore every spy/stub installed during the test. Without this the
    // global URL stub (and our fetch / document.createElement spies) leak
    // into the next suite running in the same Vitest process.
    vi.restoreAllMocks();
  });

  it('starts in idle state with no chunks or result', () => {
    expect(service.state()).toBe('idle');
    expect(service.chunks()).toEqual([]);
    expect(service.result()).toBeNull();
    expect(service.error()).toBeNull();
  });

  it('walks fetching → bundling → done on a happy-path stream', async () => {
    const events = [
      event({ type: 'session_started', session_id: 'sess1' }),
      event({ type: 'chunk_plan', total: 2 }),
      event({ type: 'chunk_start', index: 1, total: 2, from: '2026-01-01', to: '2026-02-01' }),
      event({ type: 'chunk_done', index: 1, total: 2, bars_returned: 5000 }),
      event({ type: 'chunk_start', index: 2, total: 2, from: '2026-02-02', to: '2026-03-01' }),
      event({ type: 'chunk_done', index: 2, total: 2, bars_returned: 3000 }),
      event({ type: 'fetch_complete', raw_bars: 8000, processed_bars: 7800, indicator_columns: 5 }),
      event({ type: 'bundle_start', components: ['dataset.csv', 'metadata.csv'] }),
      event({ type: 'bundle_component_done', name: 'dataset.csv' }),
      event({ type: 'bundle_component_done', name: 'metadata.csv' }),
      event({ type: 'complete', session_id: 'sess1', filename: 'SPY.zip', size_bytes: 4096 }),
    ];

    // First fetch is the SSE call; second is the auto-download GET. Stub both.
    const fetchMock = vi
      .spyOn(globalThis, 'fetch')
      .mockResolvedValueOnce(sseResponse(events))
      .mockResolvedValueOnce(new Response(new Blob(['x']), { status: 200 }));

    // Stub only the static methods on URL — never replace the URL
    // constructor itself, since other suites in the same Vitest process
    // call `new URL(...)` in their setup paths and break with
    // "URL is not a constructor" if we replace it wholesale.
    vi.spyOn(URL, 'createObjectURL').mockReturnValue('blob:fake');
    vi.spyOn(URL, 'revokeObjectURL').mockImplementation(() => undefined);
    const click = vi.fn();
    vi.spyOn(document, 'createElement').mockImplementation(((tag: string) => {
      if (tag === 'a') return { href: '', download: '', click } as unknown as HTMLAnchorElement;
      return document.createElement(tag);
    }) as typeof document.createElement);

    await service.start({ ticker: 'SPY' });

    expect(service.state()).toBe('done');
    expect(service.sessionId()).toBe('sess1');
    expect(service.result()).toEqual({ sessionId: 'sess1', filename: 'SPY.zip', sizeBytes: 4096 });
    expect(service.chunks()).toHaveLength(2);
    expect(service.chunks().every((c) => c.status === 'done')).toBe(true);
    expect(service.bundleComponents().every((c) => c.status === 'done')).toBe(true);
    expect(click).toHaveBeenCalled();
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });

  it('marks the next-up queued chunk as paced when chunk_paced fires', async () => {
    const events = [
      event({ type: 'session_started', session_id: 'sess2' }),
      event({ type: 'chunk_plan', total: 3 }),
      event({ type: 'chunk_start', index: 1, total: 3, from: 'a', to: 'b' }),
      event({ type: 'chunk_done', index: 1, total: 3, bars_returned: 100 }),
      event({ type: 'chunk_paced', wait_seconds: 9.4, label: 'aggs:SPY' }),
      // We don't close the stream, but the test inspects state mid-flight.
    ];

    vi.spyOn(globalThis, 'fetch').mockResolvedValueOnce(sseResponse(events));

    await service.start({ ticker: 'SPY' }, { downloadOnComplete: false });

    const queued = service.chunks().find((c) => c.status === 'queued');
    expect(queued).toBeDefined();
    expect(queued!.waitSeconds).toBe(9);
  });

  it('transitions to error on an explicit error event', async () => {
    const events = [
      event({ type: 'session_started', session_id: 'sess3' }),
      event({ type: 'chunk_plan', total: 1 }),
      event({ type: 'error', kind: 'http', message: 'No bars returned' }),
    ];
    vi.spyOn(globalThis, 'fetch').mockResolvedValueOnce(sseResponse(events));

    await service.start({ ticker: 'XYZ' }, { downloadOnComplete: false });

    expect(service.state()).toBe('error');
    expect(service.error()).toEqual({ kind: 'http', message: 'No bars returned' });
  });

  it('reset() returns the state machine to idle', async () => {
    const events = [
      event({ type: 'session_started', session_id: 'sess4' }),
      event({ type: 'error', kind: 'cancelled', message: 'manual abort' }),
    ];
    vi.spyOn(globalThis, 'fetch').mockResolvedValueOnce(sseResponse(events));

    await service.start({ ticker: 'SPY' }, { downloadOnComplete: false });
    expect(service.state()).toBe('error');

    service.reset();

    expect(service.state()).toBe('idle');
    expect(service.error()).toBeNull();
    expect(service.chunks()).toEqual([]);
  });

  it('progressFraction reflects done-chunks during fetching', async () => {
    const events = [
      event({ type: 'session_started', session_id: 'sess5' }),
      event({ type: 'chunk_plan', total: 4 }),
      event({ type: 'chunk_start', index: 1, total: 4, from: 'a', to: 'b' }),
      event({ type: 'chunk_done', index: 1, total: 4, bars_returned: 1 }),
      event({ type: 'chunk_start', index: 2, total: 4, from: 'b', to: 'c' }),
      event({ type: 'chunk_done', index: 2, total: 4, bars_returned: 1 }),
    ];
    vi.spyOn(globalThis, 'fetch').mockResolvedValueOnce(sseResponse(events));

    await service.start({ ticker: 'SPY' }, { downloadOnComplete: false });

    expect(service.progressFraction()).toBeCloseTo(0.5);
  });
});

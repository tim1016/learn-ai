import { Injectable, signal, computed } from '@angular/core';
import { environment } from '../../environments/environment';

/**
 * State machine for the unified Fetch & bundle flow. Mirrors the design
 * brief's run-card states A–E.
 */
export type RunState = 'idle' | 'fetching' | 'bundling' | 'done' | 'error';

export interface ChunkStatus {
  index: number;
  total: number;
  from: string;
  to: string;
  /** queued | fetching | done. ``paced`` is a transient sub-state of
   *  whichever chunk is next in line; we model it as a ``waitSeconds``
   *  field on this entry rather than a fourth status. */
  status: 'queued' | 'fetching' | 'done';
  barsReturned?: number;
  /** When > 0, this chunk is the next-up and is paused for this many
   *  seconds to stay under the plan's per-minute cap. */
  waitSeconds?: number;
}

export interface BundleComponentStatus {
  name: string;
  status: 'queued' | 'done';
}

/**
 * Per-contract progress within a bundling component (today only the
 * options-companion builder fans out enough requests to need this — each
 * contract is one Polygon call). The frontend renders this as
 * "options_calls.csv · contract 47 · O:SPY260417C00705000".
 */
export interface BundleComponentProgress {
  /** Filename in the components list, e.g. "options_calls.csv". */
  component: string;
  /** 1-based count of items processed so far in that component. */
  step: number;
  /** Optional human-readable identifier of the item just being processed. */
  label?: string;
}

export interface RunResult {
  sessionId: string;
  filename: string;
  sizeBytes: number;
}

export interface RunError {
  kind: 'cancelled' | 'http' | 'internal';
  message: string;
}

/**
 * Drives one run of the streaming dataset pipeline.
 *
 * Lifecycle:
 *   ``start(payload)`` opens an SSE-style stream against
 *   ``POST /api/dataset/generate-zip/stream``, parses JSON events, and
 *   updates signals so the run-card template can render states B/C/D/E.
 *   On ``complete`` the service auto-fetches the binary ZIP.
 *
 *   ``cancel()`` sends ``DELETE /api/dataset/run/{id}`` and waits for the
 *   worker to surface a final ``error`` event of kind ``cancelled``.
 *
 *   ``reset()`` clears state back to ``idle`` so the user can start
 *   another run.
 *
 * Why ``fetch`` + ``ReadableStream`` instead of ``EventSource``: the
 * EventSource API only supports GET requests; our run config is too big
 * to encode in a query string and POST is the only realistic verb.
 */
@Injectable({ providedIn: 'root' })
export class RunSessionService {
  private readonly _state = signal<RunState>('idle');
  private readonly _sessionId = signal<string | null>(null);
  private readonly _chunks = signal<readonly ChunkStatus[]>([]);
  private readonly _bundleComponents = signal<readonly BundleComponentStatus[]>([]);
  private readonly _bundleProgress = signal<BundleComponentProgress | null>(null);
  private readonly _result = signal<RunResult | null>(null);
  private readonly _error = signal<RunError | null>(null);
  private readonly _alsoZip = signal(false);
  private readonly _startedAt = signal<number | null>(null);

  /** AbortController for the in-flight stream so cancel() can hang up. */
  private _abort: AbortController | null = null;

  readonly state = this._state.asReadonly();
  readonly sessionId = this._sessionId.asReadonly();
  readonly chunks = this._chunks.asReadonly();
  readonly bundleComponents = this._bundleComponents.asReadonly();
  /** Live "step / label" within the component currently bundling (e.g.
   *  options_calls.csv at contract 47). Null between components. */
  readonly bundleProgress = this._bundleProgress.asReadonly();
  readonly result = this._result.asReadonly();
  readonly error = this._error.asReadonly();
  readonly alsoZip = this._alsoZip.asReadonly();

  /**
   * Aggregate progress fraction in [0, 1]. During fetch this reflects the
   * chunk count; during bundling it reflects the component count.
   */
  readonly progressFraction = computed<number>(() => {
    const state = this._state();
    if (state === 'fetching') {
      const chunks = this._chunks();
      if (chunks.length === 0) return 0;
      const done = chunks.filter((c) => c.status === 'done').length;
      return done / chunks.length;
    }
    if (state === 'bundling') {
      const components = this._bundleComponents();
      if (components.length === 0) return 0;
      const done = components.filter((c) => c.status === 'done').length;
      return done / components.length;
    }
    if (state === 'done') return 1;
    return 0;
  });

  /**
   * Rough time-remaining estimate, in seconds. Uses elapsed wall-time
   * vs. fraction complete. Returns null until the worker has produced
   * enough signal (≥ 1 chunk done, ≥ 5 s elapsed).
   */
  readonly etaSeconds = computed<number | null>(() => {
    const startedAt = this._startedAt();
    if (startedAt === null) return null;
    const fraction = this.progressFraction();
    if (fraction <= 0 || fraction >= 1) return null;
    const elapsedMs = Date.now() - startedAt;
    if (elapsedMs < 5000) return null;
    const total = elapsedMs / fraction;
    return Math.max(1, Math.round((total - elapsedMs) / 1000));
  });

  /** Start a run. Resolves once the stream is closed and (if applicable)
   *  the ZIP has been triggered for download. */
  async start(payload: Record<string, unknown>, options?: { downloadOnComplete?: boolean }): Promise<void> {
    this.reset();
    this._alsoZip.set(true);
    this._state.set('fetching');
    this._startedAt.set(Date.now());

    const ctrl = new AbortController();
    this._abort = ctrl;
    let response: Response;
    try {
      response = await fetch(`${environment.pythonServiceUrl}/api/dataset/generate-zip/stream`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
        signal: ctrl.signal,
      });
    } catch (e: unknown) {
      // Network/abort failure before the stream opens. Treat abort as a
      // cancellation so the UI shows the right state.
      const aborted = e instanceof DOMException && e.name === 'AbortError';
      this._error.set({
        kind: aborted ? 'cancelled' : 'internal',
        message: aborted ? 'Cancelled before any data arrived' : (e instanceof Error ? e.message : String(e)),
      });
      this._state.set('error');
      return;
    }

    if (!response.ok || !response.body) {
      this._error.set({ kind: 'http', message: `HTTP ${response.status}` });
      this._state.set('error');
      return;
    }

    await this._consumeSseStream(response.body, ctrl.signal);

    if (options?.downloadOnComplete !== false && this._state() === 'done') {
      await this._downloadResult();
    }
  }

  /** Hang up the stream and tell the server to stop the worker. */
  async cancel(): Promise<void> {
    const sid = this._sessionId();
    if (sid) {
      // Fire-and-forget — the worker will surface a final ``error`` event
      // of kind ``cancelled`` over the open stream.
      try {
        await fetch(`${environment.pythonServiceUrl}/api/dataset/run/${sid}`, { method: 'DELETE' });
      } catch {
        // We tried; the abort below stops the client side regardless.
      }
    }
    if (this._abort) {
      this._abort.abort();
    }
  }

  /** Move back to idle so the user can start another run. */
  reset(): void {
    this._state.set('idle');
    this._sessionId.set(null);
    this._chunks.set([]);
    this._bundleComponents.set([]);
    this._bundleProgress.set(null);
    this._result.set(null);
    this._error.set(null);
    this._alsoZip.set(false);
    this._startedAt.set(null);
    this._abort = null;
  }

  // ── SSE plumbing ──────────────────────────────────────────────────

  private async _consumeSseStream(body: ReadableStream<Uint8Array>, signal: AbortSignal): Promise<void> {
    const reader = body.getReader();
    const decoder = new TextDecoder('utf-8');
    let buffer = '';

    try {
      while (true) {
        const { value, done } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });
        // SSE events are separated by a blank line. Within one event we
        // only care about lines beginning with "data:".
        let sepIdx = buffer.indexOf('\n\n');
        while (sepIdx !== -1) {
          const raw = buffer.slice(0, sepIdx);
          buffer = buffer.slice(sepIdx + 2);
          const data = raw
            .split('\n')
            .filter((l) => l.startsWith('data:'))
            .map((l) => l.slice(5).trimStart())
            .join('\n');
          if (data) {
            try {
              this._handleEvent(JSON.parse(data));
            } catch {
              // Bad JSON from the server is unusual; skip rather than
              // taking down the whole stream consumer.
            }
          }
          sepIdx = buffer.indexOf('\n\n');
        }
      }
    } catch (e: unknown) {
      const aborted = signal.aborted || (e instanceof DOMException && e.name === 'AbortError');
      if (!aborted) {
        this._error.set({
          kind: 'internal',
          message: e instanceof Error ? e.message : String(e),
        });
        this._state.set('error');
      }
    }
  }

  private _handleEvent(event: { type: string } & Record<string, unknown>): void {
    switch (event.type) {
      case 'session_started':
        this._sessionId.set(event['session_id'] as string);
        break;
      case 'chunk_plan': {
        const total = event['total'] as number;
        const queued: ChunkStatus[] = Array.from({ length: total }, (_, i) => ({
          index: i + 1,
          total,
          from: '',
          to: '',
          status: 'queued',
        }));
        this._chunks.set(queued);
        break;
      }
      case 'chunk_start': {
        const idx = event['index'] as number;
        const from = event['from'] as string;
        const to = event['to'] as string;
        this._chunks.update((list) =>
          list.map((c) => (c.index === idx ? { ...c, status: 'fetching', from, to, waitSeconds: 0 } : c)),
        );
        break;
      }
      case 'chunk_done': {
        const idx = event['index'] as number;
        const bars = event['bars_returned'] as number;
        this._chunks.update((list) =>
          list.map((c) => (c.index === idx ? { ...c, status: 'done', barsReturned: bars, waitSeconds: 0 } : c)),
        );
        break;
      }
      case 'chunk_paced': {
        const wait = event['wait_seconds'] as number;
        // Tag the next-up queued chunk so the UI shows "paced for Ns".
        this._chunks.update((list) => {
          const out = [...list];
          const next = out.findIndex((c) => c.status === 'queued');
          if (next !== -1) out[next] = { ...out[next], waitSeconds: Math.round(wait) };
          return out;
        });
        break;
      }
      case 'fetch_complete':
        // Bars are in; the bundling phase is next, even if no companions
        // are configured (build_zip_bytes always produces dataset.csv etc.).
        this._state.set('bundling');
        break;
      case 'bundle_start': {
        const components = (event['components'] as string[]) ?? [];
        this._bundleComponents.set(components.map((name) => ({ name, status: 'queued' })));
        this._bundleProgress.set(null);
        break;
      }
      case 'bundle_progress': {
        this._bundleProgress.set({
          component: event['component'] as string,
          step: event['step'] as number,
          label: event['label'] as string | undefined,
        });
        break;
      }
      case 'bundle_component_done': {
        const name = event['name'] as string;
        this._bundleComponents.update((list) =>
          list.map((c) => (c.name === name ? { ...c, status: 'done' } : c)),
        );
        // Clear sub-component progress when its parent completes — the
        // next component's first ``bundle_progress`` will repopulate it.
        if (this._bundleProgress()?.component === name) {
          this._bundleProgress.set(null);
        }
        break;
      }
      case 'complete':
        this._result.set({
          sessionId: event['session_id'] as string,
          filename: event['filename'] as string,
          sizeBytes: event['size_bytes'] as number,
        });
        this._state.set('done');
        break;
      case 'error':
        this._error.set({
          kind: (event['kind'] as RunError['kind']) ?? 'internal',
          message: (event['message'] as string) ?? 'unknown error',
        });
        this._state.set('error');
        break;
      // dividend_adjusted, etc. — informational, ignored by the state machine.
    }
  }

  /** Pull the binary ZIP off the server and trigger a browser download. */
  private async _downloadResult(): Promise<void> {
    const result = this._result();
    if (!result) return;
    const url = `${environment.pythonServiceUrl}/api/dataset/run/${result.sessionId}/zip`;
    try {
      const response = await fetch(url);
      if (!response.ok) {
        this._error.set({ kind: 'http', message: `ZIP retrieval failed (HTTP ${response.status})` });
        this._state.set('error');
        return;
      }
      const blob = await response.blob();
      const objectUrl = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = objectUrl;
      a.download = result.filename;
      a.click();
      URL.revokeObjectURL(objectUrl);
    } catch (e: unknown) {
      this._error.set({ kind: 'internal', message: e instanceof Error ? e.message : String(e) });
      this._state.set('error');
    }
  }
}

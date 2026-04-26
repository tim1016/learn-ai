import { Injectable, computed, inject, signal } from '@angular/core';
import { JobsService } from './jobs.service';

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
  /** queued → fetching → done. ``fetching`` is set when
   *  ``bundle_component_start`` fires and lets the run-card render an
   *  in-progress visual cue for slow Polygon calls (news, financials,
   *  trades, quotes) that previously flickered queued → done. */
  status: 'queued' | 'fetching' | 'done';
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
  /** Job id minted by the .NET layer (formerly "session_id"). */
  sessionId: string;
  filename: string;
  sizeBytes: number;
  /** Public download URL — `/api/jobs/{id}/download`. */
  downloadUrl: string;
}

export interface RunError {
  kind: 'cancelled' | 'http' | 'internal';
  message: string;
}

/**
 * Drives one run of the dataset fetch + bundle pipeline.
 *
 * Lifecycle:
 *   ``start(payload)`` enqueues a ``dataset-zip`` job via :class:`JobsService`,
 *   subscribes to its event stream, and updates signals so the run-card
 *   template can render states B/C/D/E. On ``job.completed`` the service
 *   auto-fetches the binary ZIP from ``GET /api/jobs/{id}/download``.
 *
 *   ``cancel()`` calls :meth:`JobsService.cancelJob` which sets the
 *   cancel flag in Redis; the worker observes it between chunks and
 *   emits ``job.cancelled``.
 *
 *   ``reset()`` clears state back to ``idle`` so the user can start
 *   another run.
 *
 * The chunk/component-level events (``chunk_plan``, ``chunk_start``,
 * ``bundle_start``, …) are emitted unchanged by the Python worker via
 * ``ProgressEmitter.emit_event``; they flow through the framework's
 * Redis Stream → SSE pipeline transparently. This service only adds
 * lifecycle handling for the framework events (``job.started``,
 * ``job.completed``, …).
 */
@Injectable({ providedIn: 'root' })
export class RunSessionService {
  private readonly jobs = inject(JobsService);

  private readonly _state = signal<RunState>('idle');
  private readonly _sessionId = signal<string | null>(null);
  private readonly _chunks = signal<readonly ChunkStatus[]>([]);
  private readonly _bundleComponents = signal<readonly BundleComponentStatus[]>([]);
  private readonly _bundleProgress = signal<BundleComponentProgress | null>(null);
  private readonly _result = signal<RunResult | null>(null);
  private readonly _error = signal<RunError | null>(null);
  private readonly _alsoZip = signal(false);
  private readonly _startedAt = signal<number | null>(null);
  private readonly _processingIndicators = signal<{ indicatorCount: number; barCount: number } | null>(null);

  /** Subscriber handle on the active job's SSE stream. */
  private _eventSource: EventSource | null = null;
  /** Filename + size captured from the terminal event so the auto-download
   *  has what it needs without parsing the result envelope twice. */
  private _completionEnvelope: { downloadUrl: string; filename: string; sizeBytes: number } | null = null;

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
  /** Snapshot of the in-flight indicator computation phase, set when
   *  the worker emits ``processing_indicators`` and cleared on
   *  ``bundle_start``. The run-card surfaces it as a status line so
   *  the gap between the last chunk and bundling isn't silent. */
  readonly processingIndicators = this._processingIndicators.asReadonly();

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
    this._processingIndicators.set(null);

    let jobId: string;
    try {
      // Wrap the dataset payload as the ``dataset`` sub-object — the
      // Python job route accepts {job_id, dataset: {...}} and validates
      // the inner dict against DatasetGenerationRequest.
      jobId = await this.jobs.startJob('dataset-zip', { dataset: payload });
    } catch (e: unknown) {
      this._error.set({
        kind: 'internal',
        message: e instanceof Error ? e.message : String(e),
      });
      this._state.set('error');
      return;
    }
    this._sessionId.set(jobId);

    // Open our own EventSource alongside the JobsService one. The SSE
    // endpoint is read-only and supports multiple subscribers; this keeps
    // domain-specific event handling here without bloating JobsService.
    await this._consumeEventStream(jobId);

    if (options?.downloadOnComplete !== false && this._state() === 'done') {
      await this._downloadResult();
    }
  }

  /** Hang up the stream and tell the server to stop the worker. */
  async cancel(): Promise<void> {
    const sid = this._sessionId();
    if (sid) {
      try {
        await this.jobs.cancelJob(sid);
      } catch {
        // We tried; closing the EventSource below stops the client side
        // regardless. The worker's terminal job.cancelled event (or its
        // absence on retry) will surface through state.
      }
    }
    this._closeStream();
  }

  /** Move back to idle so the user can start another run. */
  reset(): void {
    this._closeStream();
    this._state.set('idle');
    this._sessionId.set(null);
    this._chunks.set([]);
    this._bundleComponents.set([]);
    this._bundleProgress.set(null);
    this._result.set(null);
    this._error.set(null);
    this._alsoZip.set(false);
    this._startedAt.set(null);
    this._processingIndicators.set(null);
    this._completionEnvelope = null;
  }

  // ── SSE plumbing ──────────────────────────────────────────────────

  private _consumeEventStream(jobId: string): Promise<void> {
    return new Promise((resolve) => {
      const source = new EventSource(`/api/jobs/${jobId}/events`);
      this._eventSource = source;

      source.onmessage = (msg) => {
        try {
          const event = JSON.parse(msg.data) as { type: string } & Record<string, unknown>;
          this._handleEvent(event);
          if (event.type === 'job.completed' || event.type === 'job.failed' || event.type === 'job.cancelled') {
            this._closeStream();
            resolve();
          }
        } catch {
          // Bad JSON from the server is unusual; skip rather than
          // taking down the whole stream consumer.
        }
      };

      source.onerror = () => {
        const state = this._state();
        // EventSource auto-reconnects unless we close it; if we already
        // saw a terminal state, the close below was scheduled and we
        // can resolve. Otherwise let it keep trying.
        if (state === 'done' || state === 'error') {
          this._closeStream();
          resolve();
        }
      };
    });
  }

  private _closeStream(): void {
    if (this._eventSource) {
      this._eventSource.close();
      this._eventSource = null;
    }
  }

  private _handleEvent(event: { type: string } & Record<string, unknown>): void {
    switch (event.type) {
      // ── Framework lifecycle events ────────────────────────────────
      case 'job.started':
        // No-op — _state was set to 'fetching' at start time.
        break;
      case 'job.phase':
        // Mirror the Python phase into our coarser run-card state. The
        // chunk/component-level events below carry the fine detail.
        if (event['phase'] === 'bundling') this._state.set('bundling');
        break;
      case 'job.completed':
        // Capture the binary's URL/filename so _downloadResult can fetch
        // it without re-querying state.
        this._completionEnvelope = {
          downloadUrl: (event['download_url'] as string) ?? `/api/jobs/${this._sessionId()}/download`,
          filename: (event['filename'] as string) ?? 'dataset.zip',
          sizeBytes: (event['size_bytes'] as number) ?? 0,
        };
        this._result.set({
          sessionId: this._sessionId() ?? '',
          filename: this._completionEnvelope.filename,
          sizeBytes: this._completionEnvelope.sizeBytes,
          downloadUrl: this._completionEnvelope.downloadUrl,
        });
        this._state.set('done');
        break;
      case 'job.failed':
        this._error.set({
          kind: 'internal',
          message: (event['message'] as string) ?? 'job failed',
        });
        this._state.set('error');
        break;
      case 'job.cancelled':
        this._error.set({
          kind: 'cancelled',
          message: (event['reason'] as string) ?? 'cancelled',
        });
        this._state.set('error');
        break;
      // job.progress, job.log: handled by the global JobsService for the
      // jobs-drawer view; nothing for us to do here.

      // ── Dataset-specific events (emitted by the Python worker via
      //    ProgressEmitter.emit_event) ─────────────────────────────────
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
        this._chunks.update((list) => {
          const out = [...list];
          const next = out.findIndex((c) => c.status === 'queued');
          if (next !== -1) out[next] = { ...out[next], waitSeconds: Math.round(wait) };
          return out;
        });
        break;
      }
      case 'fetch_complete':
        this._state.set('bundling');
        break;
      case 'bundle_start': {
        const components = (event['components'] as string[]) ?? [];
        this._bundleComponents.set(components.map((name) => ({ name, status: 'queued' })));
        this._bundleProgress.set(null);
        // Indicator phase ended once bundling begins.
        this._processingIndicators.set(null);
        break;
      }
      case 'bundle_component_start': {
        const name = event['name'] as string;
        this._bundleComponents.update((list) =>
          list.map((c) => (c.name === name ? { ...c, status: 'fetching' } : c)),
        );
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
        if (this._bundleProgress()?.component === name) {
          this._bundleProgress.set(null);
        }
        break;
      }
      case 'processing_indicators': {
        // Mid-fetch phase: bars are loaded, pandas-ta is now computing
        // indicators. Surface as a status line on the run-card so the
        // user sees something between the last chunk and bundle_start.
        const count = event['indicator_count'] as number;
        const bars = event['bar_count'] as number;
        this._processingIndicators.set({ indicatorCount: count, barCount: bars });
        break;
      }
      // dividend_adjusted and other informational events — ignored by
      // the state machine but visible in the global jobs-drawer feed.
    }
  }

  /** Pull the binary ZIP off the server and trigger a browser download. */
  private async _downloadResult(): Promise<void> {
    const envelope = this._completionEnvelope;
    if (!envelope) return;
    try {
      const response = await fetch(envelope.downloadUrl);
      if (!response.ok) {
        this._error.set({ kind: 'http', message: `ZIP retrieval failed (HTTP ${response.status})` });
        this._state.set('error');
        return;
      }
      const blob = await response.blob();
      const objectUrl = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = objectUrl;
      a.download = envelope.filename;
      a.click();
      URL.revokeObjectURL(objectUrl);
    } catch (e: unknown) {
      this._error.set({ kind: 'internal', message: e instanceof Error ? e.message : String(e) });
      this._state.set('error');
    }
  }
}

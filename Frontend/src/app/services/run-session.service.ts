import { Injectable, computed, inject, signal } from '@angular/core';
import { JobsService } from './jobs.service';
import type {
  RunDockLevel,
  RunDockSource,
  RunDockState,
} from '../shared/run-dock/run-dock-source';

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

/** A single line in the run dock's running event log. */
export interface RunLogEntry {
  /** Monotonic id for tracking — derived from index + ts so two entries
   *  in the same millisecond stay distinct. */
  id: string;
  /** Wall-clock millis when the entry was appended on the client. */
  timestamp: number;
  /** Severity drives the color stripe in the dock. */
  level: 'info' | 'success' | 'warn' | 'error';
  /** Single-character glyph rendered before the message. */
  glyph: string;
  /** Single-line summary. Pre-formatted; the dock just prints it. */
  message: string;
}

/** Cap on the rolling FIFO log buffer. Tuned high enough that one heavy
 *  fetch (hundreds of chunks + bundle components) doesn't roll its own
 *  earlier entries off the screen, low enough to keep the dock fast. */
const RUN_LOG_MAX_LINES = 500;

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
export class RunSessionService implements RunDockSource {
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
  /** Captured from the worker's ``fetch_complete`` event — the headline
   *  numbers for the run (raw bars Polygon returned, post-processed bars,
   *  indicator-column count). Persists through the bundle phase and into
   *  ``done`` so the run-card can show what the dataset.csv will hold. */
  private readonly _fetchSummary = signal<{ rawBars: number; processedBars: number; indicatorColumns: number } | null>(null);

  /** Rolling FIFO log of every SSE event the run pipeline emits. Capped
   *  at RUN_LOG_MAX_LINES; entries persist across runs (the dock is a
   *  "what's happened lately" view, not a per-run wipe). */
  private readonly _log = signal<readonly RunLogEntry[]>([]);
  /** Monotonic id seed so distinct entries in the same millisecond stay
   *  unique under @for track expressions. */
  private _logSeq = 0;

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
  readonly fetchSummary = this._fetchSummary.asReadonly();
  readonly log = this._log.asReadonly();

  /** Wipe the log. The dock's "Clear" button calls this; ordinary
   *  resets between runs leave the buffer alone so the user keeps the
   *  context of the previous run while the next one starts. */
  clearLog(): void {
    this._log.set([]);
  }

  /** Append one entry to the FIFO log. Trims oldest when over the cap. */
  private _appendLog(level: RunLogEntry['level'], glyph: string, message: string): void {
    const entry: RunLogEntry = {
      id: `${Date.now()}-${++this._logSeq}`,
      timestamp: Date.now(),
      level,
      glyph,
      message,
    };
    this._log.update((current) => {
      const next = [...current, entry];
      return next.length > RUN_LOG_MAX_LINES ? next.slice(-RUN_LOG_MAX_LINES) : next;
    });
  }

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

  // ── RunDockSource contract ─────────────────────────────────────────
  // These computeds project this service's internal state machine onto
  // the generic shape the shared run dock consumes. The dock is
  // deliberately unaware of "fetching" vs "bundling" — that's a
  // data-lab concern. The dock only sees idle / active / done / error.

  readonly dockState = computed<RunDockState>(() => {
    const s = this._state();
    if (s === 'idle') return 'idle';
    if (s === 'done') return 'done';
    if (s === 'error') return 'error';
    return 'active';
  });

  readonly headline = computed<string>(() => {
    const state = this._state();
    const result = this._result();
    const error = this._error();
    const chunks = this._chunks();
    const components = this._bundleComponents();
    if (state === 'idle') return 'idle — no run in flight';
    if (state === 'fetching') {
      if (chunks.length === 0) return 'fetching · planning chunks';
      const done = chunks.filter((c) => c.status === 'done').length;
      const fetching = chunks.find((c) => c.status === 'fetching');
      const idx = fetching?.index ?? Math.min(done + 1, chunks.length);
      return `fetching · chunk ${idx} of ${chunks.length}`;
    }
    if (state === 'bundling') {
      if (components.length === 0) return 'bundling · packaging';
      const done = components.filter((c) => c.status === 'done').length;
      return `bundling · ${done} of ${components.length} components`;
    }
    if (state === 'done' && result) return `done · ${result.filename}`;
    if (state === 'error' && error) return `error · ${error.message}`;
    return state;
  });

  readonly headlineLevel = computed<RunDockLevel>(() => {
    const state = this._state();
    if (state === 'done') return 'success';
    if (state === 'error') {
      return this._error()?.kind === 'cancelled' ? 'warn' : 'error';
    }
    return 'info';
  });

  readonly progressPercent = computed<number | null>(() => {
    const s = this._state();
    if (s === 'idle' || s === 'error') return null;
    return Math.round(this.progressFraction() * 100);
  });

  readonly etaText = computed<string | null>(() => {
    const eta = this.etaSeconds();
    if (eta === null) return null;
    if (eta < 60) return `~${eta} s`;
    const mins = Math.floor(eta / 60);
    const secs = eta % 60;
    return `~${mins} m ${secs.toString().padStart(2, '0')} s`;
  });

  readonly canCancel = computed<boolean>(() => {
    const s = this._state();
    return s === 'fetching' || s === 'bundling';
  });

  /** Start a run. Resolves once the stream is closed and (if applicable)
   *  the ZIP has been triggered for download. */
  async start(payload: Record<string, unknown>, options?: { downloadOnComplete?: boolean }): Promise<void> {
    this.reset();
    this._alsoZip.set(true);
    this._state.set('fetching');
    this._startedAt.set(Date.now());
    this._processingIndicators.set(null);
    const ticker = (payload as { ticker?: string }).ticker ?? '?';
    const fromDate = (payload as { from_date?: string }).from_date ?? '?';
    const toDate = (payload as { to_date?: string }).to_date ?? '?';
    this._appendLog('info', '▸', `starting run · ${ticker} · ${fromDate} → ${toDate}`);

    let jobId: string;
    try {
      jobId = await this.jobs.startJob('dataset-zip', { dataset: payload });
    } catch (e: unknown) {
      const message = e instanceof Error ? e.message : String(e);
      this._error.set({ kind: 'internal', message });
      this._state.set('error');
      this._appendLog('error', '✗', `failed to start job: ${message}`);
      return;
    }
    this._sessionId.set(jobId);
    this._appendLog('info', 'ⓘ', `job id ${jobId}`);

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
    this._fetchSummary.set(null);
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
        this._appendLog('info', '▸', 'run started');
        break;
      case 'job.phase':
        if (event['phase'] === 'bundling') this._state.set('bundling');
        this._appendLog('info', '⚙', `phase: ${event['phase']}`);
        break;
      case 'job.completed': {
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
        const mb = (this._completionEnvelope.sizeBytes / 1024 / 1024).toFixed(1);
        this._appendLog('success', '✓', `run complete · ${this._completionEnvelope.filename} · ${mb} MB`);
        break;
      }
      case 'job.failed': {
        const message = (event['message'] as string) ?? 'job failed';
        this._error.set({ kind: 'internal', message });
        this._state.set('error');
        this._appendLog('error', '✗', `failed: ${message}`);
        break;
      }
      case 'job.cancelled': {
        const reason = (event['reason'] as string) ?? 'cancelled';
        this._error.set({ kind: 'cancelled', message: reason });
        this._state.set('error');
        this._appendLog('warn', '⏹', `cancelled: ${reason}`);
        break;
      }
      case 'job.log': {
        // Framework structured logs forwarded over SSE — surface them
        // verbatim in the dock so the "wealth of information" the worker
        // emits actually reaches the user. Map the Python logging level
        // to dock severity.
        const level = ((event['level'] as string) ?? 'info').toLowerCase();
        const message = (event['message'] as string) ?? '';
        const dockLevel: RunLogEntry['level'] =
          level === 'error' || level === 'critical' ? 'error' :
          level === 'warning' || level === 'warn' ? 'warn' :
          'info';
        const glyph = dockLevel === 'error' ? '✗' : dockLevel === 'warn' ? '⚠' : 'ⓘ';
        this._appendLog(dockLevel, glyph, message);
        break;
      }

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
        this._appendLog('info', 'ⓘ', `planning ${total} chunk${total === 1 ? '' : 's'}`);
        break;
      }
      case 'chunk_start': {
        const idx = event['index'] as number;
        const from = event['from'] as string;
        const to = event['to'] as string;
        const total = event['total'] as number | undefined;
        this._chunks.update((list) =>
          list.map((c) => (c.index === idx ? { ...c, status: 'fetching', from, to, waitSeconds: 0 } : c)),
        );
        this._appendLog('info', '▸', `chunk ${idx}${total ? ` of ${total}` : ''}: ${from} → ${to}`);
        break;
      }
      case 'chunk_done': {
        const idx = event['index'] as number;
        const bars = event['bars_returned'] as number;
        this._chunks.update((list) =>
          list.map((c) => (c.index === idx ? { ...c, status: 'done', barsReturned: bars, waitSeconds: 0 } : c)),
        );
        this._appendLog('success', '✓', `chunk ${idx} · ${bars.toLocaleString()} bars`);
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
        this._appendLog('warn', '⏸', `pacing ${Math.round(wait)} s for plan rate-limit`);
        break;
      }
      case 'fetch_complete': {
        const raw = (event['raw_bars'] as number) ?? 0;
        const processed = (event['processed_bars'] as number) ?? 0;
        const cols = (event['indicator_columns'] as number) ?? 0;
        this._fetchSummary.set({ rawBars: raw, processedBars: processed, indicatorColumns: cols });
        this._state.set('bundling');
        this._appendLog(
          'success',
          '✓',
          `fetch complete · raw=${raw.toLocaleString()} → processed=${processed.toLocaleString()} · ${cols} indicator col${cols === 1 ? '' : 's'}`,
        );
        break;
      }
      case 'bundle_start': {
        const components = (event['components'] as string[]) ?? [];
        this._bundleComponents.set(components.map((name) => ({ name, status: 'queued' })));
        this._bundleProgress.set(null);
        this._processingIndicators.set(null);
        this._appendLog('info', '▸', `bundling ${components.length} component${components.length === 1 ? '' : 's'}: ${components.join(', ')}`);
        break;
      }
      case 'bundle_component_start': {
        const name = event['name'] as string;
        this._bundleComponents.update((list) =>
          list.map((c) => (c.name === name ? { ...c, status: 'fetching' } : c)),
        );
        this._appendLog('info', '▸', `bundling ${name}`);
        break;
      }
      case 'bundle_progress': {
        const component = event['component'] as string;
        const step = event['step'] as number;
        const label = event['label'] as string | undefined;
        this._bundleProgress.set({ component, step, label });
        this._appendLog('info', '·', `${component} step ${step}${label ? ` · ${label}` : ''}`);
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
        this._appendLog('success', '✓', `${name} bundled`);
        break;
      }
      case 'processing_indicators': {
        const count = event['indicator_count'] as number;
        const bars = event['bar_count'] as number;
        this._processingIndicators.set({ indicatorCount: count, barCount: bars });
        this._appendLog('info', 'ⓘ', `processing ${count} indicator${count === 1 ? '' : 's'} × ${bars.toLocaleString()} bars`);
        break;
      }
      case 'dividend_adjusted': {
        const events = (event['events'] as number) ?? 0;
        const bars = (event['bars'] as number) ?? 0;
        this._appendLog('info', 'ⓘ', `dividend adjustment · ${events} event${events === 1 ? '' : 's'} over ${bars.toLocaleString()} bars`);
        break;
      }
      default:
        // Unknown / future event types still surface in the log so the
        // user gets the "wealth of information" without us silently
        // dropping novel events.
        this._appendLog('info', '·', `${event.type}`);
    }
  }

  /** Pull the binary ZIP off the server and trigger a browser download. */
  private async _downloadResult(): Promise<void> {
    const envelope = this._completionEnvelope;
    if (!envelope) return;
    try {
      const response = await fetch(envelope.downloadUrl);
      if (!response.ok) {
        const message = `ZIP retrieval failed (HTTP ${response.status})`;
        this._error.set({ kind: 'http', message });
        this._state.set('error');
        this._appendLog('error', '✗', message);
        return;
      }
      const blob = await response.blob();
      const objectUrl = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = objectUrl;
      a.download = envelope.filename;
      a.click();
      URL.revokeObjectURL(objectUrl);
      this._appendLog('success', '⤓', `downloaded ${envelope.filename}`);
    } catch (e: unknown) {
      const message = e instanceof Error ? e.message : String(e);
      this._error.set({ kind: 'internal', message });
      this._state.set('error');
      this._appendLog('error', '✗', `download failed: ${message}`);
    }
  }
}

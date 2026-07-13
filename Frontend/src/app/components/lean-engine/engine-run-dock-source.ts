import { Injectable, computed, effect, inject, signal } from '@angular/core';
import { JobsService, JobState } from '../../services/jobs.service';
import type {
  RunDockLevel,
  RunDockSource,
  RunDockState,
  RunLogEntry,
  RunDockMeta,
} from '../../shared/run-dock/run-dock-source';

/** Engine-lab job types this dock surfaces. Keeping the set explicit avoids
 *  the dock reacting to
 *  data-lab's `dataset-zip` job while a user navigates between labs. */
const ENGINE_JOB_TYPES = new Set<string>(['engine_backtest', 'lean_engine_run']);

const RUN_LOG_MAX_LINES = 500;

/**
 * Maps `JobsService` state for engine-related jobs onto the generic
 * `RunDockSource` contract the shared dock consumes.
 *
 * Picks the most-recently-started engine job (active or terminal) as the
 * "current" job. The accumulated log buffer captures every `recentLogs`
 * entry as it flows in — `JobsService` only keeps the last few per job
 * for the drawer summary, so we maintain our own rolling FIFO here to
 * preserve full history across the dock's lifetime.
 */
@Injectable()
export class EngineRunDockSource implements RunDockSource {
  private readonly jobs = inject(JobsService);

  private readonly _log = signal<readonly RunLogEntry[]>([]);
  /** Per-job high-water-mark for structured SSE event ids we've already
   *  folded in. ``JobsService.recentEvents`` is a rolling window, so an
   *  array index is not stable after the service trims older entries. */
  private readonly _lastEventIdByJob = new Map<string, string>();

  private readonly engineJobs = computed(() =>
    this.jobs.jobs().filter((job) => ENGINE_JOB_TYPES.has(job.type)),
  );

  private readonly currentJob = computed<JobState | null>(() => {
    const jobs = this.engineJobs();
    if (jobs.length === 0) return null;
    // Keep an active run ahead of a terminal sibling, then prefer the most
    // recently updated run within the same lifecycle class.
    return jobs.reduce((latest, j) => {
      const latestActive = latest.status === 'queued' || latest.status === 'running';
      const jobActive = j.status === 'queued' || j.status === 'running';
      if (jobActive !== latestActive) return jobActive ? j : latest;
      const lt = latest.finishedAt ?? latest.startedAt ?? 0;
      const jt = j.finishedAt ?? j.startedAt ?? 0;
      return jt >= lt ? j : latest;
    });
  });

  readonly dockState = computed<RunDockState>(() => {
    const j = this.currentJob();
    if (!j) return 'idle';
    if (j.status === 'completed') return 'done';
    if (j.status === 'failed' || j.status === 'cancelled') return 'error';
    return 'active';
  });

  readonly headline = computed<string>(() => {
    const j = this.currentJob();
    if (!j) return 'idle — no engine run in flight';
    if (j.status === 'completed') return `done · ${j.type}`;
    if (j.status === 'failed') {
      return `error · ${j.errorMessage ?? j.errorCode ?? 'failed'}`;
    }
    if (j.status === 'cancelled') return 'cancelled';
    const phase = j.phaseLabel ?? j.phase ?? 'starting';
    return `${j.type} · ${phase}`;
  });

  readonly headlineLevel = computed<RunDockLevel>(() => {
    const j = this.currentJob();
    if (!j) return 'info';
    if (j.status === 'completed') return 'success';
    if (j.status === 'cancelled') return 'warn';
    if (j.status === 'failed') return 'error';
    return 'info';
  });

  readonly progressPercent = computed<number | null>(() => {
    const j = this.currentJob();
    if (!j) return null;
    if (j.status === 'completed') return 100;
    if (j.current === undefined || j.total === undefined || j.total === 0) {
      return null;
    }
    // Clamp to [0,100] — a worker that briefly reports current > total
    // (off-by-one at boundaries, late progress event after the run
    // exited, etc.) shouldn't make the dock render an invalid bar.
    const pct = Math.round((j.current / j.total) * 100);
    return Math.min(100, Math.max(0, pct));
  });

  readonly etaText = computed<string | null>(() => {
    const j = this.currentJob();
    if (!j || !j.startedAt || j.status !== 'running') return null;
    const pct = this.progressPercent();
    if (pct === null || pct <= 0 || pct >= 100) return null;
    const elapsedMs = Date.now() - j.startedAt;
    if (elapsedMs < 5000) return null;
    const totalMs = elapsedMs * (100 / pct);
    const remainingMs = Math.max(0, totalMs - elapsedMs);
    const secs = Math.max(1, Math.round(remainingMs / 1000));
    if (secs < 60) return `~${secs} s`;
    const m = Math.floor(secs / 60);
    const s = secs % 60;
    return `~${m} m ${s.toString().padStart(2, '0')} s`;
  });

  readonly canCancel = computed<boolean>(() => {
    const j = this.currentJob();
    return j !== null && (j.status === 'queued' || j.status === 'running');
  });

  readonly log = this._log.asReadonly();

  readonly runMeta = computed<RunDockMeta | null>(() => {
    const job = this.currentJob();
    if (!job) return null;
    return {
      runId: job.id,
      runType: job.type,
      // Prefer the humanised label so the dock renders "Running indicators",
      // not the raw backend phase id "running_indicators" (JobState contract).
      phase: job.phaseLabel ?? job.phase ?? null,
      startedAt: job.startedAt ?? null,
      finishedAt: job.finishedAt ?? null,
      current: job.current ?? null,
      total: job.total ?? null,
      unit: job.unit ?? null,
    };
  });

  clearLog(): void {
    this._log.set([]);
    // Leave _lastEventIdByJob populated so already-folded entries from the
    // current job's rolling window don't re-appear on the next update.
  }

  async cancel(): Promise<void> {
    const j = this.currentJob();
    if (!j) return;
    await this.jobs.cancelJob(j.id);
  }

  constructor() {
    // Fold every structured SSE event into the visible timeline. This keeps
    // phase and progress events observable instead of showing only job.log.
    effect(() => {
      const entries: RunLogEntry[] = [];
      for (const job of this.engineJobs()) {
        const events = job.recentEvents ?? [];
        const lastEventId = this._lastEventIdByJob.get(job.id);
        const lastEventIndex = lastEventId
          ? events.findIndex((event) => event.id === lastEventId)
          : -1;
        const fresh = lastEventId && lastEventIndex === -1
          ? events
          : events.slice(lastEventIndex + 1);
        if (fresh.length === 0) continue;
        this._lastEventIdByJob.set(job.id, fresh[fresh.length - 1].id);
        const engineLabel = job.type === 'lean_engine_run' ? 'LEAN' : 'Python';
        entries.push(...fresh.map((event) => ({
          id: `${job.id}-${event.id}`,
          timestamp: event.timestamp,
          level: mapLogLevel(event.level),
          glyph: glyphForEvent(event.type, event.level),
          message: `[${engineLabel}] ${event.summary}`,
        })));
      }
      if (entries.length === 0) return;
      entries.sort((left, right) => left.timestamp - right.timestamp);
      this._log.update((curr) => {
        const next = [...curr, ...entries];
        return next.length > RUN_LOG_MAX_LINES
          ? next.slice(-RUN_LOG_MAX_LINES)
          : next;
      });
    });
  }
}

function mapLogLevel(raw: string): RunDockLevel {
  if (raw === 'error') return 'error';
  if (raw === 'warn' || raw === 'warning') return 'warn';
  if (raw === 'success') return 'success';
  return 'info';
}

function glyphForEvent(type: string, raw: string): string {
  if (raw === 'error') return '✗';
  if (raw === 'warn' || raw === 'warning') return '⚠';
  if (raw === 'success') return '✓';
  if (type === 'job.phase') return '◆';
  if (type === 'job.progress') return '↗';
  if (type === 'job.started') return '▶';
  if (type === 'job.completed') return '✓';
  return '·';
}

import { Injectable, computed, effect, inject, signal } from '@angular/core';
import { JobsService, JobState } from '../../services/jobs.service';
import type {
  RunDockLevel,
  RunDockSource,
  RunDockState,
  RunLogEntry,
} from '../../shared/run-dock/run-dock-source';

/** Engine-lab job types this dock surfaces. Add `lean_engine_run` here
 *  once #470 lands. Keeping the set explicit avoids the dock reacting to
 *  data-lab's `dataset-zip` job while a user navigates between labs. */
const ENGINE_JOB_TYPES = new Set<string>(['engine_backtest']);

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
  /** Per-job high-water-mark for log sequence numbers we've already
   *  folded in. Lets us dedupe when ``JobsService.recentLogs`` updates
   *  by appending the newest entry on top of the previous rolling
   *  window — without growing memory unbounded as the run's seq
   *  counter climbs. `recentLogs` entries arrive in increasing-seq
   *  order, so a single ``maxSeen`` per job is sufficient. */
  private readonly _maxSeqByJob = new Map<string, number>();

  private readonly currentJob = computed<JobState | null>(() => {
    const jobs = this.jobs.jobs().filter((j) => ENGINE_JOB_TYPES.has(j.type));
    if (jobs.length === 0) return null;
    // Most recent by startedAt (jobs without startedAt sort as oldest).
    return jobs.reduce((latest, j) => {
      const lt = latest.startedAt ?? 0;
      const jt = j.startedAt ?? 0;
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

  clearLog(): void {
    this._log.set([]);
    // Leave _maxSeqByJob populated so already-folded entries from the
    // current job's rolling window don't re-appear on the next update.
  }

  async cancel(): Promise<void> {
    const j = this.currentJob();
    if (!j) return;
    await this.jobs.cancelJob(j.id);
  }

  constructor() {
    // Fold new log entries from the current job's rolling `recentLogs`
    // window into our own accumulated buffer, deduped by sequence number.
    effect(() => {
      const j = this.currentJob();
      if (!j) return;
      const maxSeen = this._maxSeqByJob.get(j.id) ?? -1;
      const fresh = j.recentLogs.filter((l) => l.seq > maxSeen);
      if (fresh.length === 0) return;
      // ``recentLogs`` arrives in seq order — take the last entry's
      // seq as the new high-water-mark in one O(1) lookup. Falls back
      // to a defensive max over the fresh slice in case a future
      // change relaxes the ordering guarantee.
      const newMax = fresh.reduce((m, l) => (l.seq > m ? l.seq : m), maxSeen);
      this._maxSeqByJob.set(j.id, newMax);
      const entries: RunLogEntry[] = fresh.map((l) => ({
        id: `${j.id}-${l.seq}`,
        timestamp: l.ts,
        level: mapLogLevel(l.level),
        glyph: glyphForLevel(l.level),
        message: l.message,
      }));
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

function glyphForLevel(raw: string): string {
  if (raw === 'error') return '✗';
  if (raw === 'warn' || raw === 'warning') return '⚠';
  if (raw === 'success') return '✓';
  return '·';
}

import { DestroyRef, Injectable, computed, inject, signal } from '@angular/core';

import {
  BackfillJobRunner,
  BackfillSubmissionError,
  type BackfillJobRun,
} from '../../../shared/data-lake/backfill-job-runner';
import { BackfillDayEvent, BackfillFailure, DataRunSpec } from '../../../shared/data-lake';
import { BACKFILL_JOB_TYPE } from '../../../shared/data-lake/backfill-job-type';

export { BACKFILL_JOB_TYPE };

export type BackfillPhase =
  | 'idle'
  | 'submitting'
  | 'running'
  | 'completed'
  | 'failed'
  | 'cancelled';

export interface BackfillProgress {
  readonly current: number;
  readonly total: number;
  readonly unit: string;
  readonly message: string | null;
}

export interface BackfillError {
  /** A code when the job framework gave one, else a synthesized reason code. Render through `receiptLabel`. */
  readonly code: string;
  readonly message: string;
}

type SseEvent = { readonly type: string } & Readonly<Record<string, unknown>>;

function asString(value: unknown): string | null {
  return typeof value === 'string' ? value : null;
}

function asNumber(value: unknown): number | null {
  return typeof value === 'number' && Number.isFinite(value) ? value : null;
}

function toFailure(raw: unknown): BackfillFailure | null {
  if (typeof raw !== 'object' || raw === null) return null;
  const record = raw as Record<string, unknown>;
  const reason = asString(record['reason']);
  if (reason === null) return null;
  return {
    artifact_kind: asString(record['artifact_kind']) ?? '',
    symbol: asString(record['symbol']),
    trading_date_ms: asNumber(record['trading_date_ms']),
    data_type: asString(record['data_type']),
    reason,
    detail: asString(record['detail']),
    provider_status_code: asNumber(record['provider_status_code']),
    attempt_count: asNumber(record['attempt_count']) ?? 0,
  };
}

function toDayEvent(event: SseEvent): BackfillDayEvent | null {
  const tradingDateMs = asNumber(event['trading_date_ms']);
  const dayIndex = asNumber(event['day_index']);
  const totalDays = asNumber(event['total_days']);
  if (tradingDateMs === null || dayIndex === null || totalDays === null) return null;
  const rawFailures = Array.isArray(event['failures']) ? event['failures'] : [];
  return {
    trading_date_ms: tradingDateMs,
    day_index: dayIndex,
    total_days: totalDays,
    days_remaining: asNumber(event['days_remaining']) ?? Math.max(0, totalDays - dayIndex),
    fetched_count: asNumber(event['fetched_count']) ?? 0,
    reused_count: asNumber(event['reused_count']) ?? 0,
    failures: rawFailures.map(toFailure).filter((f): f is BackfillFailure => f !== null),
  };
}

/**
 * Drives one data-lake backfill from submission to a terminal event.
 *
 * Submission goes through the shared `BackfillJobRunner` — the one
 * submission path and one `JobsService` stream subscription per job in the
 * app (#1856) — and every domain frame the runner forwards lands in
 * `ingestEvent` here. The runner alone owns `job.*`; this store owns only
 * the per-day receipts. `ingestEvent` is public so the fold is unit-testable without
 * an `EventSource` (jsdom has none); the SSE handler only parses a frame
 * and routes it here.
 */
@Injectable()
export class DataLakeBackfillStore {
  private readonly runner = inject(BackfillJobRunner);
  private readonly destroyRef = inject(DestroyRef);

  private readonly localPhaseState = signal<'idle' | 'submitting' | 'failed'>('idle');
  private readonly jobIdState = signal<string | null>(null);
  private readonly daysState = signal<readonly BackfillDayEvent[]>([]);
  private readonly submissionErrorState = signal<BackfillError | null>(null);
  private readonly reattachedState = signal(false);
  private readonly runState = signal<BackfillJobRun | null>(null);

  readonly phase = computed<BackfillPhase>(() => {
    const run = this.runState();
    if (run === null) return this.localPhaseState();
    switch (run.state().kind) {
      case 'running':
        return 'running';
      case 'completed':
        return 'completed';
      case 'failed':
        return 'failed';
      case 'cancelled':
        return 'cancelled';
      case 'detached':
        return 'idle';
    }
  });
  readonly jobId = this.jobIdState.asReadonly();
  readonly progress = computed<BackfillProgress | null>(() => {
    return this.runState()?.state().progress ?? null;
  });
  readonly days = this.daysState.asReadonly();
  readonly error = computed<BackfillError | null>(() => {
    const lifecycle = this.runState()?.state();
    if (lifecycle?.kind === 'failed') {
      return { code: lifecycle.code, message: lifecycle.message };
    }
    return this.submissionErrorState();
  });
  /** True when this run was adopted mid-flight rather than started here. */
  readonly reattached = this.reattachedState.asReadonly();

  readonly running = computed(() => {
    const phase = this.phase();
    return phase === 'submitting' || phase === 'running';
  });

  readonly failures = computed<readonly BackfillFailure[]>(() =>
    this.daysState().flatMap((day) => day.failures),
  );

  readonly fetchedCount = computed(() =>
    this.daysState().reduce((total, day) => total + day.fetched_count, 0),
  );

  readonly reusedCount = computed(() =>
    this.daysState().reduce((total, day) => total + day.reused_count, 0),
  );

  constructor() {
    this.destroyRef.onDestroy(() => this.closeStream());
  }

  async start(spec: DataRunSpec): Promise<void> {
    this.reset();
    this.localPhaseState.set('submitting');
    try {
      const run = await this.runner.start(spec, {
        onDomainEvent: (event) => this.ingestEvent(event),
      });
      this.openStream(run);
    } catch (error) {
      if (!(error instanceof BackfillSubmissionError)) throw error;
      this.localPhaseState.set('failed');
      this.submissionErrorState.set({
        code:
          error.classifiedKind === 'rejected'
            ? (error.classifiedReason ?? 'submission_failed')
            : 'submission_failed',
        message: error.message,
      });
      return;
    }
    this.jobIdState.set(this.runState()?.jobId ?? null);
  }

  /**
   * Adopt a backfill the server is already running.
   *
   * This store is provided by the panel, so navigating away destroys it
   * while the worker keeps going; coming back would otherwise show an idle
   * form beside a job that is still writing sessions to disk.
   *
   * Nothing is reconstructed by hand. `GET /api/jobs/{id}/events` with no
   * `Last-Event-ID` replays the job's whole Redis stream from the start
   * before it begins tailing (`JobsApi.StreamJobEventsAsync`), so the
   * ordinary fold rebuilds the progress tick, the per-day receipts and the
   * failures from the run's own events — and `data_lake.backfill_day` is
   * keyed on `day_index`, so a session cannot land twice. The only run long
   * enough to have been trimmed would need more than `MAX_STREAM_LENGTH`
   * (50k) events, which a day-per-session backfill cannot reach inside the
   * stream's 24h TTL; a shorter history simply renders as fewer rows, never
   * as invented ones.
   */
  reattach(jobId: string): void {
    if (this.jobIdState() === jobId) return;
    this.reset();
    this.jobIdState.set(jobId);
    this.reattachedState.set(true);
    this.openStream(
      this.runner.observe(jobId, {
        onDomainEvent: (event) => this.ingestEvent(event),
      }),
    );
  }

  async cancel(): Promise<void> {
    await this.runState()?.cancel();
  }

  reset(): void {
    this.closeStream();
    this.localPhaseState.set('idle');
    this.jobIdState.set(null);
    this.daysState.set([]);
    this.submissionErrorState.set(null);
    this.reattachedState.set(false);
  }

  /** Folds one already-parsed domain frame. Unknown event types are ignored. */
  ingestEvent(event: SseEvent): void {
    if (event.type !== 'data_lake.backfill_day') return;
    const day = toDayEvent(event);
    if (day === null) return;
    // A reconnect may redeliver the frame straddling the drop. Key on the
    // day index so a replayed session is corrected instead of duplicated.
    this.daysState.update((days) => {
      const existing = days.findIndex((candidate) => candidate.day_index === day.day_index);
      if (existing === -1) return [...days, day];
      const next = [...days];
      next[existing] = day;
      return next;
    });
  }

  private openStream(run: BackfillJobRun): void {
    this.runState.set(run);
  }

  private closeStream(): void {
    this.runState()?.detach();
    this.runState.set(null);
  }
}

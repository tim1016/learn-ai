import { DestroyRef, Injectable, computed, inject, signal } from '@angular/core';

import { JobsService, type JobStreamEvent } from '../../../services/jobs.service';
import { classifyDataLakeError } from './data-lake.service';
import type { BackfillDayEvent, BackfillFailure, DataRunSpec } from './data-lake.types';

/** The public job type the .NET jobs framework forwards to the data-lake router. */
export const BACKFILL_JOB_TYPE = 'data_lake_backfill';

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

const TERMINAL_EVENTS = new Set(['job.completed', 'job.failed', 'job.cancelled']);

/**
 * Drives one data-lake backfill from submission to a terminal event.
 *
 * Submission goes through `JobsService.startJob`, which is what mints the
 * job id and writes its initial Redis state; the worker on the Python side
 * then streams `job.*` lifecycle events *and* the domain-specific
 * `data_lake.backfill_day` payload over the same Redis-backed SSE channel.
 * `JobsService` deliberately understands only the `job.*` verbs, so this
 * store rides `JobsService.onEvent()` (#1856) — the same one stream
 * `RunSessionService` rides for the dataset bundler — rather than each
 * domain consumer opening its own second `EventSource` to the same
 * endpoint. Domain handling (the fold below) stays local to this store
 * rather than bloating the shared registry.
 *
 * `ingestEvent` is public so the fold is unit-testable without an
 * `EventSource` (jsdom has none); the SSE handler only parses a frame and
 * routes it here.
 */
@Injectable()
export class DataLakeBackfillStore {
  private readonly jobs = inject(JobsService);
  private readonly destroyRef = inject(DestroyRef);

  private readonly phaseState = signal<BackfillPhase>('idle');
  private readonly jobIdState = signal<string | null>(null);
  private readonly progressState = signal<BackfillProgress | null>(null);
  private readonly daysState = signal<readonly BackfillDayEvent[]>([]);
  private readonly errorState = signal<BackfillError | null>(null);
  private readonly reattachedState = signal(false);

  readonly phase = this.phaseState.asReadonly();
  readonly jobId = this.jobIdState.asReadonly();
  readonly progress = this.progressState.asReadonly();
  readonly days = this.daysState.asReadonly();
  readonly error = this.errorState.asReadonly();
  /** True when this run was adopted mid-flight rather than started here. */
  readonly reattached = this.reattachedState.asReadonly();

  readonly running = computed(() => {
    const phase = this.phaseState();
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

  private unsubscribeEvents: (() => void) | null = null;

  constructor() {
    this.destroyRef.onDestroy(() => this.closeStream());
  }

  async start(spec: DataRunSpec): Promise<void> {
    this.reset();
    this.phaseState.set('submitting');
    let jobId: string;
    try {
      jobId = await this.jobs.startJob(BACKFILL_JOB_TYPE, { spec });
    } catch (error) {
      const classified = classifyDataLakeError(error);
      this.phaseState.set('failed');
      this.errorState.set({
        code: classified.kind === 'rejected' ? classified.reason : 'submission_failed',
        message: classified.message,
      });
      return;
    }
    this.jobIdState.set(jobId);
    this.phaseState.set('running');
    this.openStream(jobId);
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
    this.phaseState.set('running');
    this.openStream(jobId);
  }

  async cancel(): Promise<void> {
    const jobId = this.jobIdState();
    if (jobId === null) return;
    await this.jobs.cancelJob(jobId);
  }

  reset(): void {
    this.closeStream();
    this.phaseState.set('idle');
    this.jobIdState.set(null);
    this.progressState.set(null);
    this.daysState.set([]);
    this.errorState.set(null);
    this.reattachedState.set(false);
  }

  /** Folds one already-parsed SSE frame. Unknown event types are ignored. */
  ingestEvent(event: SseEvent): void {
    switch (event.type) {
      case 'job.started':
        this.phaseState.set('running');
        break;
      case 'job.progress': {
        const current = asNumber(event['current']);
        const total = asNumber(event['total']);
        if (current === null || total === null) break;
        this.progressState.set({
          current,
          total,
          unit: asString(event['unit']) ?? 'days',
          message: asString(event['message']),
        });
        break;
      }
      case 'data_lake.backfill_day': {
        const day = toDayEvent(event);
        if (day === null) break;
        // A reconnect replays the stream from the last delivered id, and
        // the framework may redeliver the frame that straddled the drop.
        // Key on the day index so a replayed session is corrected in
        // place instead of appearing twice in the run's own receipt.
        this.daysState.update((days) => {
          const existing = days.findIndex((d) => d.day_index === day.day_index);
          if (existing === -1) return [...days, day];
          const next = [...days];
          next[existing] = day;
          return next;
        });
        break;
      }
      case 'job.completed':
        this.phaseState.set('completed');
        break;
      case 'job.failed':
        this.phaseState.set('failed');
        this.errorState.set({
          code: asString(event['code']) ?? 'internal_error',
          message: asString(event['message']) ?? 'The backfill job failed.',
        });
        break;
      case 'job.cancelled':
        this.phaseState.set('cancelled');
        break;
    }
    if (TERMINAL_EVENTS.has(event.type)) this.closeStream();
  }

  private openStream(jobId: string): void {
    this.unsubscribeEvents = this.jobs.onEvent(jobId, (event: JobStreamEvent) => this.ingestEvent(event));
  }

  private closeStream(): void {
    this.unsubscribeEvents?.();
    this.unsubscribeEvents = null;
  }
}

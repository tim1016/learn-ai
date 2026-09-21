import { Injectable, inject, signal, type Signal } from '@angular/core';

import { JobsService, type JobStreamEvent } from '../../services/jobs.service';
import { classifyDataLakeError, type DataRunSpec } from './index';
import { BACKFILL_JOB_TYPE } from './backfill-job-type';

/** One parsed `job.progress` tick, verbatim from the frame. */
export interface BackfillProgressTick {
  readonly current: number;
  readonly total: number;
  readonly unit: string;
  readonly message: string | null;
}

/**
 * How a run's observation ended. `completed`/`failed`/`cancelled` mirror
 * the job framework's terminal frames; `detached` means a consumer stopped
 * observing (or the whole gate was torn down) before any terminal frame
 * arrived — the job itself may still be running server-side.
 */
export type BackfillTerminalEvent =
  | { readonly kind: 'completed' }
  | { readonly kind: 'failed'; readonly code: string; readonly message: string }
  | { readonly kind: 'cancelled' }
  | { readonly kind: 'detached' };

export interface BackfillJobRun {
  readonly jobId: string;
  /** Seen a `job.started` frame (including in a reattach's stream replay). */
  readonly started: Signal<boolean>;
  /** The latest `job.progress` tick, `null` before the first one lands. */
  readonly progress: Signal<BackfillProgressTick | null>;
  /** Resolves exactly once: a terminal frame, or `{kind: 'detached'}`. */
  readonly terminal: Promise<BackfillTerminalEvent>;
  /** Stop observing. A delivered terminal still reaches `terminal`. */
  readonly detach: () => void;
  /** Ask the server to cancel the job. */
  readonly cancel: () => Promise<void>;
}

export interface BackfillRunHooks {
  /**
   * Every frame — `job.*` verbs included — is forwarded here after the
   * runner has folded it, so a consumer projecting richer UI state (the
   * backfill panel's per-day receipts) sees the same stream this runner
   * terminates on. A terminal frame is forwarded before the run resolves,
   * which is why the panel's fold may call `detach()` from inside its own
   * handler without losing the verdict.
   */
  onEvent(event: JobStreamEvent): void;
}

/** `startJob` refused the spec — classified, so consumers map their own copy. */
export class BackfillSubmissionError extends Error {
  constructor(
    readonly classifiedKind: string,
    readonly classifiedReason: string | null,
    message: string,
  ) {
    super(message);
    this.name = 'BackfillSubmissionError';
  }
}

function asString(value: unknown): string | null {
  return typeof value === 'string' ? value : null;
}

function asNumber(value: unknown): number | null {
  return typeof value === 'number' && Number.isFinite(value) ? value : null;
}

/**
 * The one place that knows how a data-lake backfill job runs: submission
 * through `JobsService.startJob`, the `job.*` lifecycle fold over the
 * job's single Redis-backed SSE stream, terminal detection, and teardown.
 *
 * Consumers project their own UI state onto a run — the Observatory panel
 * folds the domain `data_lake.backfill_day` frames for its receipt table,
 * the symbol picker's ensure-coverage gate reads `progress`/`terminal` and
 * nothing else — so there is exactly one submission path, one stream
 * subscription per job, and one definition of "terminal" in the app.
 */
@Injectable({ providedIn: 'root' })
export class BackfillJobRunner {
  private readonly jobs = inject(JobsService);

  /** Submits the spec, then folds the accepted job's stream. */
  async start(spec: DataRunSpec, hooks?: BackfillRunHooks): Promise<BackfillJobRun> {
    let jobId: string;
    try {
      jobId = await this.jobs.startJob(BACKFILL_JOB_TYPE, { spec });
    } catch (error) {
      const classified = classifyDataLakeError(error);
      throw new BackfillSubmissionError(
        classified.kind,
        classified.kind === 'rejected' ? classified.reason : null,
        classified.message,
      );
    }
    return this.observe(jobId, hooks);
  }

  /**
   * Fold a job this process did not submit — the panel's reattach path.
   * With no `Last-Event-ID`, the stream replays the job's whole history
   * first, so `started`/`progress` rebuild from the run's own frames.
   */
  observe(jobId: string, hooks?: BackfillRunHooks): BackfillJobRun {
    const startedState = signal(false);
    const progressState = signal<BackfillProgressTick | null>(null);
    let unsubscribe: (() => void) | null = null;
    let settled = false;
    let resolveTerminal!: (terminal: BackfillTerminalEvent) => void;
    const terminal = new Promise<BackfillTerminalEvent>((resolve) => {
      resolveTerminal = resolve;
    });

    const finish = (event: BackfillTerminalEvent) => {
      if (settled) return;
      settled = true;
      unsubscribe?.();
      unsubscribe = null;
      resolveTerminal(event);
    };

    unsubscribe = this.jobs.onEvent(jobId, (event) => {
      switch (event.type) {
        case 'job.started':
          startedState.set(true);
          break;
        case 'job.progress': {
          const current = asNumber(event['current']);
          const total = asNumber(event['total']);
          if (current === null || total === null) break;
          progressState.set({
            current,
            total,
            unit: asString(event['unit']) ?? 'days',
            message: asString(event['message']),
          });
          break;
        }
        case 'job.completed':
          finish({ kind: 'completed' });
          break;
        case 'job.failed':
          finish({
            kind: 'failed',
            code: asString(event['code']) ?? 'internal_error',
            message: asString(event['message']) ?? 'The backfill job failed.',
          });
          break;
        case 'job.cancelled':
          finish({ kind: 'cancelled' });
          break;
      }
      hooks?.onEvent(event);
    });

    return {
      jobId,
      started: startedState.asReadonly(),
      progress: progressState.asReadonly(),
      terminal,
      detach: () => finish({ kind: 'detached' }),
      cancel: () => this.jobs.cancelJob(jobId),
    };
  }
}

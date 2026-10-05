import { DestroyRef, Injectable, computed, inject, signal } from '@angular/core';

import { DataLakeService, classifyDataLakeError, rootFailureOf, toBackfillDayEvent, tradeBackfillSpec, type BackfillFailure } from '../../shared/data-lake';
import { BackfillJobRunner, type BackfillJobRun, type BackfillProgressTick } from '../../shared/data-lake/backfill-job-runner';
import { TickerCatalogService } from '../../shared/ticker-catalog';
import type { BackfillRemedy } from './golden-search.types';

/** How a backfill ended: the job did not run to its end, or it did, with the provider's reason when a day fetched nothing. */
type PlanBackfillEnd =
  | { readonly kind: 'failed'; readonly remedy: BackfillRemedy; readonly message: string }
  | { readonly kind: 'finished'; readonly remedy: BackfillRemedy; readonly failure: BackfillFailure | null };

/** How the backfill of a refused plan's missing sessions stands: running, or how the last one ended. */
export type PlanBackfillState =
  | {
      readonly kind: 'running';
      readonly remedy: BackfillRemedy;
      /** The server has taken the job, so it can be told to stop. */
      readonly accepted: boolean;
      readonly progress: BackfillProgressTick | null;
    }
  | PlanBackfillEnd;

/** Whether `ran` was a backfill of every session `asked` names: the same symbol and lake tree, over a window that holds `asked`'s. */
export function covers(ran: BackfillRemedy, asked: BackfillRemedy): boolean {
  return (
    ran.symbol === asked.symbol &&
    ran.price_adjustment_mode === asked.price_adjustment_mode &&
    ran.start_trading_date_ms <= asked.start_trading_date_ms &&
    asked.end_trading_date_ms <= ran.end_trading_date_ms
  );
}

/**
 * Runs the backfill a plan refusal names, for the Plan step's footer: the
 * same lake job the Data Lake page submits, over the window the server
 * named. One runs at a time. Whether it fetched the sessions is the plan
 * check's to say, so the form checks the plan again when `fill` resolves;
 * this only reports how the job ended and which backfills were tried.
 */
@Injectable()
export class GoldenSearchPlanBackfill {
  private readonly lake = inject(DataLakeService);
  private readonly runner = inject(BackfillJobRunner);
  private readonly catalog = inject(TickerCatalogService);

  private readonly job = signal<BackfillJobRun | null>(null);
  private readonly phase = signal<{ readonly kind: 'running'; readonly remedy: BackfillRemedy } | PlanBackfillEnd | null>(null);
  /** Every backfill that ran to an end here. */
  private readonly ended = signal<readonly BackfillRemedy[]>([]);
  /** The form is gone: a job still running keeps writing on the server, unobserved. */
  private left = false;

  readonly state = computed<PlanBackfillState | null>(() => {
    const phase = this.phase();
    if (phase?.kind !== 'running') return phase;
    const job = this.job();
    return { ...phase, accepted: job !== null, progress: job?.state().progress ?? null };
  });

  constructor() {
    inject(DestroyRef).onDestroy(() => {
      this.left = true;
      this.job()?.detach();
    });
  }

  /** Whether a backfill of every session `remedy` names has run to an end here. */
  tried(remedy: BackfillRemedy): boolean {
    return this.ended().some((ran) => covers(ran, remedy));
  }

  /**
   * Runs `remedy`'s backfill. Resolves true when it has ended or was
   * cancelled, and the lake's holdings are being read again; false when one
   * was already running, or the form left first.
   */
  async fill(remedy: BackfillRemedy): Promise<boolean> {
    if (this.phase()?.kind === 'running') return false;
    this.job.set(null);
    this.phase.set({ kind: 'running', remedy });
    const end = await this.run(remedy);
    if (this.left) return false;
    // The pickers' held days and gaps are read off the lake, which the job wrote to, cancelled or not.
    this.catalog.viewFor(remedy.price_adjustment_mode).reload();
    // A run this form stopped watching was cancelled, and what the footer then says is `cancel`'s to write.
    if (end !== null) {
      this.ended.update((ended) => [...ended, remedy]);
      this.phase.set(end);
    }
    return true;
  }

  /**
   * Stops the running backfill: this form stops watching it at once, so a
   * job whose end never arrives cannot hold the footer, and the server is
   * then asked to stop it. The sessions it already fetched stay.
   */
  async cancel(): Promise<void> {
    const phase = this.phase();
    const job = this.job();
    if (phase?.kind !== 'running' || job === null) return;
    this.phase.set(null);
    job.detach();
    try {
      await job.cancel();
    } catch (error) {
      // Nothing to tell a form that has gone, or one already showing a newer backfill.
      if (this.left || this.phase() !== null) return;
      const { message } = classifyDataLakeError(error);
      this.phase.set({ kind: 'failed', remedy: phase.remedy, message: `it was cancelled here, but the server could not be told to stop it, so it may still be running. ${message}` });
    }
  }

  /** How the job ended; null when this form stopped watching it. Never rejects: a throw would leave the footer on "running". */
  private async run(remedy: BackfillRemedy): Promise<PlanBackfillEnd | null> {
    const failed = (message: string): PlanBackfillEnd => ({ kind: 'failed', remedy, message });
    try {
      const defaults = await this.lake.backfillDefaults();
      if (defaults.kind !== 'ok') return failed(defaults.message);
      const digest = defaults.value.lean_image_digest;
      if (digest === null) return failed('The data plane has no pinned LEAN image digest, so a backfill spec cannot be composed.');
      // The worker walks oldest session first, so the last day that reports a failure names what ended or emptied the run.
      let failure: BackfillFailure | null = null;
      const job = await this.runner.start(tradeBackfillSpec(remedy, digest), {
        onDomainEvent: (event) => {
          failure = rootFailureOf(toBackfillDayEvent(event)?.failures ?? []) ?? failure;
        },
      });
      this.job.set(job);
      // The form left while the server was taking the job.
      if (this.left) job.detach();
      const terminal = await job.terminal;
      switch (terminal.kind) {
        case 'completed':
          return { kind: 'finished', remedy, failure };
        case 'failed':
          return failed(terminal.message);
        case 'cancelled':
          return failed('The backfill was cancelled.');
        case 'detached':
          return null;
      }
    } catch (error) {
      // A refused submission arrives here in the data plane's own words.
      return failed(error instanceof Error ? error.message : String(error));
    }
  }
}

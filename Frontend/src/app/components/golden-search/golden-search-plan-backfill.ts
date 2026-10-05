import { DestroyRef, Injectable, computed, inject, signal } from '@angular/core';

import { DataLakeService, rootFailureOf, toBackfillDayEvent, tradeBackfillSpec, type BackfillFailure } from '../../shared/data-lake';
import { BackfillJobRunner, type BackfillJobRun, type BackfillProgressTick } from '../../shared/data-lake/backfill-job-runner';
import { TickerCatalogService } from '../../shared/ticker-catalog';
import type { BackfillRemedy } from './golden-search.types';

/** How a backfill ended: the job did not run to its end, or it did, with the provider's reason when a day fetched nothing. */
type PlanBackfillEnd =
  | { readonly kind: 'failed'; readonly symbol: string; readonly message: string }
  | { readonly kind: 'finished'; readonly symbol: string; readonly failure: BackfillFailure | null };

/** How the backfill of a refused plan's missing sessions stands: running, or how the last one ended. */
export type PlanBackfillState = { readonly kind: 'running'; readonly symbol: string; readonly progress: BackfillProgressTick | null } | PlanBackfillEnd;

function remedyKey(remedy: BackfillRemedy): string {
  return `${remedy.price_adjustment_mode}|${remedy.symbol}`;
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
  private readonly phase = signal<{ readonly kind: 'running'; readonly symbol: string } | PlanBackfillEnd | null>(null);
  /** The symbol and lake tree of every backfill that ran to an end here. */
  private readonly ended = signal<ReadonlySet<string>>(new Set());
  /** The form is gone: a job still running keeps writing on the server, unobserved. */
  private left = false;

  readonly state = computed<PlanBackfillState | null>(() => {
    const phase = this.phase();
    return phase?.kind === 'running' ? { ...phase, progress: this.job()?.state().progress ?? null } : phase;
  });

  constructor() {
    inject(DestroyRef).onDestroy(() => {
      this.left = true;
      this.job()?.detach();
    });
  }

  /** Whether a backfill of `remedy`'s symbol and lake tree has run to an end here. */
  tried(remedy: BackfillRemedy): boolean {
    return this.ended().has(remedyKey(remedy));
  }

  /**
   * Runs `remedy`'s backfill. Resolves true when it has ended and the lake's
   * holdings are being read again; false when one was already running, or
   * the form left before this one ended.
   */
  async fill(remedy: BackfillRemedy): Promise<boolean> {
    if (this.phase()?.kind === 'running') return false;
    this.job.set(null);
    this.phase.set({ kind: 'running', symbol: remedy.symbol });
    const end = await this.run(remedy);
    if (end === null) return false;
    // The pickers' held days and gaps are read off the lake, which the job just wrote.
    this.catalog.viewFor(remedy.price_adjustment_mode).reload();
    this.ended.update((ended) => new Set(ended).add(remedyKey(remedy)));
    this.phase.set(end);
    return true;
  }

  /** Never rejects: a throw would leave the footer on "running" for what is a failure to report. Null when the form left. */
  private async run(remedy: BackfillRemedy): Promise<PlanBackfillEnd | null> {
    const { symbol } = remedy;
    const failed = (message: string): PlanBackfillEnd => ({ kind: 'failed', symbol, message });
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
      if (this.left) {
        job.detach();
        return null;
      }
      this.job.set(job);
      const terminal = await job.terminal;
      switch (terminal.kind) {
        case 'completed':
          return { kind: 'finished', symbol, failure };
        case 'failed':
          return failed(terminal.message);
        case 'cancelled':
          return failed('The backfill was cancelled.');
        case 'detached':
          return null;
      }
    } catch (error) {
      if (this.left) return null;
      // A refused submission arrives here in the data plane's own words.
      return failed(error instanceof Error ? error.message : String(error));
    }
  }
}

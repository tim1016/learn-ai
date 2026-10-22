import { Injectable, inject } from '@angular/core';

import { DataLakeService } from './data-lake.service';
import { rootFailureOf, toBackfillDayEvent } from './backfill-day-event';
import { BackfillJobRunner, BackfillSubmissionError, type BackfillJobRun } from './backfill-job-runner';
import type { BackfillDefaults, BackfillFailure } from './data-lake.types';
import { tradeBackfillSpec, type TradeBackfillWindow } from './trade-backfill-spec';

export type TradeBackfillEnd =
  | { readonly kind: 'completed'; readonly failure: BackfillFailure | null }
  | { readonly kind: 'failed'; readonly code: string; readonly message: string }
  | { readonly kind: 'detached' };

type WindowSelection = TradeBackfillWindow | Extract<TradeBackfillEnd, { kind: 'failed' }> | null;

/** The shared defaults, submission and terminal fold for one symbol's trade-bar window (#2900). */
@Injectable({ providedIn: 'root' })
export class TradeBackfillRunner {
  private readonly lake = inject(DataLakeService);
  private readonly runner = inject(BackfillJobRunner);

  /**
   * The consumer chooses its window from the defaults, or returns null if it left.
   * Once accepted, the consumer takes ownership of the handle; false means it has
   * already detached or cancelled it. Completion says nothing about lake coverage.
   */
  async run(
    selectWindow: (defaults: BackfillDefaults) => WindowSelection,
    accepted: (job: BackfillJobRun) => boolean | Promise<boolean>,
  ): Promise<TradeBackfillEnd> {
    const failed = (code: string, message: string): TradeBackfillEnd => ({ kind: 'failed', code, message });
    try {
      const defaults = await this.lake.backfillDefaults();
      if (defaults.kind !== 'ok') return failed('backfill_defaults_unavailable', defaults.message);
      const digest = defaults.value.lean_image_digest;
      if (digest === null) return failed('backfill_digest_missing',
        'The data plane has no pinned LEAN image digest, so a backfill spec cannot be composed.');
      const window = selectWindow(defaults.value);
      if (window === null) return { kind: 'detached' };
      if ('kind' in window) return window;
      // The last day carrying a real cause explains a run that completed without usable bars.
      let failure: BackfillFailure | null = null;
      const job = await this.runner.start(tradeBackfillSpec(window, digest), {
        onDomainEvent: (event) => {
          failure = rootFailureOf(toBackfillDayEvent(event)?.failures ?? []) ?? failure;
        },
      });
      if (!await accepted(job)) return { kind: 'detached' };
      const terminal = await job.terminal;
      switch (terminal.kind) {
        case 'completed': return { kind: 'completed', failure };
        case 'failed': return failed(terminal.code, terminal.message);
        case 'cancelled': return failed('backfill_cancelled', 'The backfill was cancelled.');
        case 'detached': return terminal;
      }
    } catch (error) {
      return failed(error instanceof BackfillSubmissionError ? 'backfill_submission_failed' : 'backfill_run_error',
        error instanceof Error ? error.message : String(error));
    }
  }
}

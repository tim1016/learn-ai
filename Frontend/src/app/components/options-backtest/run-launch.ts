import { computed, signal } from '@angular/core';

import { extractServerMessage } from '../broker/operation-error';
import { RunRefusedError, type RunQuery } from './options-backtest.service';
import type { RunListItem, RunStartRequest } from './options-backtest.types';

/** What a launch needs from the service: start a run, and find runs. */
export interface RunApi {
  startRun(request: RunStartRequest): Promise<string>;
  runs(query: RunQuery): Promise<RunListItem[]>;
}

/**
 * Starting a run, and finding again one that is going. It holds the run on
 * screen (`current`), whether a start is under way, the server's reason when
 * it refused the range before any job began, and any other reason a run could
 * not be started. While a start is under way or the run on screen is going,
 * `busy` is true and a start does nothing: one run at a time from this page.
 */
export class RunLaunch {
  private readonly shown = signal<RunListItem | null>(null);
  readonly current = this.shown.asReadonly();
  private readonly under = signal(false);
  readonly starting = this.under.asReadonly();
  private readonly refused = signal<string | null>(null);
  readonly refusal = this.refused.asReadonly();
  private readonly failed = signal<string | null>(null);
  readonly problem = this.failed.asReadonly();
  readonly busy = computed(() => this.under() || this.shown()?.status === 'running');

  private generation = 0;

  constructor(private readonly api: RunApi) {}

  /** Starts the run and shows its record. Does nothing while one is going. */
  async start(request: RunStartRequest): Promise<void> {
    if (this.busy()) return;
    this.generation += 1;
    this.under.set(true);
    this.refused.set(null);
    this.failed.set(null);
    try {
      const jobId = await this.api.startRun(request);
      const [run] = await this.api.runs({ jobId });
      if (run === undefined) {
        this.failed.set('The backtest was started, but its record could not be found. Look for it in History.');
      } else {
        this.shown.set(run);
      }
    } catch (error) {
      if (error instanceof RunRefusedError) this.refused.set(error.message);
      else this.failed.set(extractServerMessage(error, 'The backtest could not be started. Try again.'));
    } finally {
      this.under.set(false);
    }
  }

  /** On coming back to the page: shows the run that is going, if one is, instead of leaving Start free to begin another. */
  async takeUp(): Promise<void> {
    if (this.busy()) return;
    const generation = this.generation;
    let runs: RunListItem[];
    try {
      runs = await this.api.runs({});
    } catch (error) {
      if (generation === this.generation) this.failed.set(`Whether a backtest is already running could not be checked: ${extractServerMessage(error, 'the history did not answer')}.`);
      return;
    }
    const going = runs.find((run) => run.status === 'running');
    if (going !== undefined && generation === this.generation && !this.busy()) this.shown.set(going);
  }

  /** The run on screen was read again: keep what is newer. */
  update(run: RunListItem): void {
    if (this.shown()?.id === run.id) this.shown.set(run);
  }

  clearRefusal(): void {
    this.refused.set(null);
  }
}

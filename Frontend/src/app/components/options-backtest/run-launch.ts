import { computed, DestroyRef, signal } from '@angular/core';

import { RecordPoller } from '../../shared/research-record/record-poller';
import { extractServerMessage } from '../broker/operation-error';
import { RunRefusedError, type RunQuery } from './options-backtest.service';
import type { RunListItem, RunStartRequest } from './options-backtest.types';

/** What a launch needs from the service: start a run, and find runs. */
export interface RunApi {
  startRun(request: RunStartRequest): Promise<string>;
  runs(query: RunQuery): Promise<RunListItem[]>;
}

/** Something the owner must be told about a run that is not on screen: a start that failed, a record not found yet, a check that could not be made. */
export interface LaunchProblem {
  readonly kind: 'not_started' | 'record_unread' | 'unchecked';
  readonly message: string;
}

/**
 * Starting a run, and finding again one that is going. It holds the run on
 * screen (`current`), whether a start is under way, the server's reason when
 * it refused the range before any job began, and any other reason a run could
 * not be started. Once a job has started it is going, whatever happens to the
 * look for its record: `busy` stays true until the record is found and says the
 * run is over, a start does nothing meanwhile, and nothing says the backtest
 * was not started. One run at a time from this page.
 */
export class RunLaunch {
  private readonly shown = signal<RunListItem | null>(null);
  readonly current = this.shown.asReadonly();
  private readonly under = signal(false);
  readonly starting = this.under.asReadonly();
  /** The job that started, until the record of the run it made has been found. */
  private readonly startedJob = signal<string | null>(null);
  private readonly refused = signal<string | null>(null);
  readonly refusal = this.refused.asReadonly();
  private readonly failed = signal<LaunchProblem | null>(null);
  readonly problem = this.failed.asReadonly();
  readonly busy = computed(() => this.under() || this.startedJob() !== null || this.shown()?.status === 'running');

  private readonly poller: RecordPoller;
  private generation = 0;
  private gone = false;

  constructor(
    private readonly api: RunApi,
    destroyRef: DestroyRef,
    private readonly lookAgainMs: number,
  ) {
    this.poller = new RecordPoller(destroyRef);
    destroyRef.onDestroy(() => (this.gone = true));
  }

  /** Starts the run and shows its record. Does nothing while one is going. */
  async start(request: RunStartRequest): Promise<void> {
    if (this.busy()) return;
    this.generation += 1;
    this.under.set(true);
    this.refused.set(null);
    this.failed.set(null);
    let jobId: string;
    try {
      jobId = await this.api.startRun(request);
    } catch (error) {
      this.under.set(false);
      if (error instanceof RunRefusedError) this.refused.set(error.message);
      else this.failed.set({ kind: 'not_started', message: extractServerMessage(error, 'The backtest could not be started. Try again.') });
      return;
    }
    // From here the job is going. No failure to find its record may say it was not started, or free Start.
    this.startedJob.set(jobId);
    this.under.set(false);
    await this.find(jobId);
  }

  /** On coming back to the page: shows the run that is going, if one is, instead of leaving Start free to begin another. */
  async takeUp(): Promise<void> {
    if (this.busy()) return;
    const generation = this.generation;
    let runs: RunListItem[];
    try {
      runs = await this.api.runs({});
    } catch (error) {
      if (generation === this.generation) {
        this.failed.set({ kind: 'unchecked', message: `Whether a backtest is already running could not be checked: ${extractServerMessage(error, 'the history did not answer')}.` });
      }
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

  /** Looks for the record of the run a job made; when it is not there, or cannot be read, says so and looks again in a few seconds. */
  private async find(jobId: string): Promise<void> {
    let run: RunListItem | undefined;
    let why: string | null = null;
    try {
      [run] = await this.api.runs({ jobId });
    } catch (error) {
      why = extractServerMessage(error, 'the history did not answer');
    }
    if (this.gone || this.startedJob() !== jobId) return;
    if (run !== undefined) {
      this.shown.set(run);
      this.startedJob.set(null);
      this.failed.set(null);
      this.poller.stop();
      return;
    }
    const reason = why === null ? '' : ` (${why})`;
    this.failed.set({ kind: 'record_unread', message: `The backtest was started, but its record cannot be read yet${reason}. The page will look again.` });
    this.poller.schedule(this.lookAgainMs, () => void this.find(jobId));
  }
}

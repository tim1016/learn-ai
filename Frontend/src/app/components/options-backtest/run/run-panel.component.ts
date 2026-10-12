import { afterNextRender, ChangeDetectionStrategy, Component, computed, effect, ElementRef, inject, Injector, input, output, resource, signal, untracked, viewChild } from '@angular/core';
import { ButtonModule } from 'primeng/button';

import { JobsService } from '../../../services/jobs.service';
import { AssetIdentityComponent } from '../../../shared/asset-identity';
import { ReceiptLabelPipe } from '../../../shared/pipes/receipt-label.pipe';
import { TimestampDisplayComponent } from '../../../shared/timestamp';
import { extractServerMessage } from '../../broker/operation-error';
import { SetupSentencesComponent } from '../cards/setup-sentences.component';
import { OptionsBacktestService, RunRefusedError } from '../options-backtest.service';
import type { RunDetail, RunListItem, ServedSetup, SetupSummary } from '../options-backtest.types';
import { progressView } from '../run-words';
import { JobProgressComponent } from './job-progress.component';
import { RunFailureComponent } from './run-failure.component';
import { RunResultComponent } from './run-result.component';

/**
 * One run on screen, whichever tab holds it. It is given the run's record and
 * reads the run itself. A run that is going shows its progress, from the shared
 * jobs service, with Cancel; when the job ends the run is read again and what
 * the run's record says decides what is shown: the result, why it failed, or
 * that it was cancelled and nothing was saved. A run opened from History can
 * also show the setup it ran, and offer it back to the form. Each read of the
 * run carries a generation, so an answer to an earlier read never replaces a later one.
 */
@Component({
  selector: 'app-options-backtest-run-panel',
  imports: [
    ButtonModule,
    AssetIdentityComponent,
    ReceiptLabelPipe,
    TimestampDisplayComponent,
    SetupSentencesComponent,
    JobProgressComponent,
    RunFailureComponent,
    RunResultComponent,
  ],
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './run-panel.component.html',
  styleUrl: './run-panel.component.scss',
})
export class RunPanelComponent {
  private readonly service = inject(OptionsBacktestService);
  private readonly jobs = inject(JobsService);
  private readonly injector = inject(Injector);

  readonly run = input.required<RunListItem>();
  /** Show the sentences of the setup this run used, and offer Use this setup (a run opened from History). */
  readonly showSetup = input(false);
  /** The run was read again and its status is not the one it was given. */
  readonly updated = output<RunDetail>();
  readonly useSetup = output<ServedSetup>();

  private readonly record = signal<RunDetail | null>(null);
  private readonly readFailure = signal<string | null>(null);
  private generation = 0;
  protected readonly detail = this.record.asReadonly();
  protected readonly readProblem = this.readFailure.asReadonly();
  /** What the run is: the record read last, or the record it was given until one has been read. */
  protected readonly status = computed(() => this.detail()?.status ?? this.run().status);
  protected readonly failure = computed(() => this.detail()?.failure ?? this.run().failure ?? null);

  private readonly runId = computed(() => this.run().id);
  private readonly job = computed(() => this.jobs.job(this.run().job_id) ?? null);
  protected readonly progress = computed(() => progressView(this.job()));
  protected readonly cancelling = signal(false);
  protected readonly cancelProblem = signal<string | null>(null);

  // The setup the run used, in the server's sentences, for a run opened from History.
  private readonly setupRead = resource({
    params: () => (this.showSetup() ? (this.detail()?.setup ?? undefined) : undefined),
    loader: ({ params }) => this.service.describe(params),
  });
  protected readonly setupWords = computed<SetupSummary | null>(() => (this.setupRead.hasValue() ? this.setupRead.value() : null));
  protected readonly setupProblem = computed(() =>
    this.setupRead.error() === undefined ? null : 'The setup of this run could not be put into words. You can still use it.',
  );

  protected readonly canUseSetup = computed(() => this.detail()?.setup != null);
  /** Why a run that was read has no setup the page can open, in the server's words; null while the run has one or is not yet read. */
  protected readonly noSetupWhy = computed(() => {
    const detail = this.detail();
    if (detail === null || detail.setup != null) return null;
    return detail.setup_cannot_open_reason ?? 'The server did not send the setup this run used.';
  });

  private readonly heading = viewChild<ElementRef<HTMLElement>>('heading');
  private reloadedFor: string | null = null;
  private searchedFor: string | null = null;

  constructor() {
    // A run to show: read it, and start afresh if the run given is another one (the Backtest tab shows each new run in this panel).
    effect(() => {
      const id = this.runId();
      untracked(() => {
        this.cancelling.set(false);
        this.cancelProblem.set(null);
        void this.load(id);
      });
    });
    // The job ended: what the run's record says is the word on how it went.
    effect(() => {
      const status = this.job()?.status;
      if (status === 'completed' || status === 'failed' || status === 'cancelled') untracked(() => this.jobEnded());
    });
    // The record says the run is going and this tab has not heard of the job: ask for the jobs that are going, then read the run again.
    effect(() => {
      const run = this.run();
      if (this.status() === 'running' && this.jobs.job(run.job_id) === undefined) untracked(() => void this.findTheJob(run.job_id));
    });
    // Tell the page when the record says something other than it was given, so a finished run stops blocking a new start.
    effect(() => {
      const detail = this.detail();
      if (detail !== null && detail.status !== untracked(() => this.run().status)) this.updated.emit(detail);
    });
  }

  /** Puts the keyboard on the run's heading, where the owner has just asked for it. */
  focusHeading(): void {
    afterNextRender(() => this.heading()?.nativeElement.focus(), { injector: this.injector });
  }

  protected reread(): void {
    void this.load(this.run().id);
  }

  private async load(id: number): Promise<void> {
    this.generation += 1;
    const generation = this.generation;
    if (this.record()?.id !== id) this.record.set(null);
    try {
      const detail = await this.service.run(id);
      if (generation !== this.generation) return;
      this.record.set(detail);
      this.readFailure.set(null);
    } catch (error) {
      if (generation !== this.generation) return;
      this.readFailure.set(error instanceof RunRefusedError ? error.message : extractServerMessage(error, 'The run could not be read.'));
    }
  }

  protected async cancel(): Promise<void> {
    this.cancelProblem.set(null);
    this.cancelling.set(true);
    try {
      await this.jobs.cancelJob(this.run().job_id);
    } catch (error) {
      this.cancelling.set(false);
      this.cancelProblem.set(extractServerMessage(error, 'The cancel could not be asked for. Try again.'));
    }
  }

  protected use(): void {
    const setup = this.detail()?.setup;
    if (setup != null) this.useSetup.emit(setup);
  }

  private jobEnded(): void {
    const jobId = this.run().job_id;
    if (this.reloadedFor === jobId || this.status() !== 'running') return;
    this.reloadedFor = jobId;
    void this.load(this.run().id);
  }

  private async findTheJob(jobId: string): Promise<void> {
    if (this.searchedFor === jobId) return;
    this.searchedFor = jobId;
    await this.jobs.refreshActive();
    if (this.jobs.job(jobId) === undefined) void this.load(this.run().id);
  }
}

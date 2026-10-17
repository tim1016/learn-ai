import { afterNextRender, ChangeDetectionStrategy, Component, computed, DestroyRef, effect, ElementRef, inject, Injector, input, output, resource, signal, untracked, viewChild } from '@angular/core';
import { ButtonModule } from 'primeng/button';

import { JobsService } from '../../../services/jobs.service';
import { AssetIdentityComponent } from '../../../shared/asset-identity';
import { ReceiptLabelPipe } from '../../../shared/pipes/receipt-label.pipe';
import { RecordPoller } from '../../../shared/research-record/record-poller';
import { TimestampDisplayComponent } from '../../../shared/timestamp';
import { extractServerMessage } from '../../broker/operation-error';
import { SetupSentencesComponent } from '../cards/setup-sentences.component';
import { OptionsBacktestService, RunRefusedError } from '../options-backtest.service';
import type { RunDetail, RunListItem, ServedSetup, SetupSummary } from '../options-backtest.types';
import { RUN_POLL_MS } from '../run-poll';
import { progressView } from '../run-words';
import { JobProgressComponent } from './job-progress.component';
import { GoldenSetupApprovalComponent } from './golden-setup-approval.component';
import { RunFailureComponent } from './run-failure.component';
import { RunResultComponent } from './run-result.component';

/**
 * One run on screen, whichever tab holds it. It is given the run's record and
 * reads the run itself, and reads it again every few seconds for as long as
 * the record says the run is going: the record is the truth, and the job's
 * stream only decorates it (the phase, the count, the latest line) and says
 * sooner when the job is over. What the record says decides what is shown: the
 * result, why it failed, or that it was cancelled and nothing was saved. A run
 * opened from History can also show the setup it ran, and offer it back to the
 * form. Each read of the run carries a generation, so an answer to an earlier
 * read never replaces a later one.
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
    GoldenSetupApprovalComponent,
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
  private readonly host = inject<ElementRef<HTMLElement>>(ElementRef);
  private readonly pollMs = inject(RUN_POLL_MS);
  private readonly poller = new RecordPoller(inject(DestroyRef));

  readonly run = input.required<RunListItem>();
  /** Show the sentences of the setup this run used, and offer Use this setup (a run opened from History). */
  readonly showSetup = input(false);
  /** Why the form cannot take a setup just now, in words (the session clock is unread); null when it can. */
  readonly setupBlockedWhy = input<string | null>(null);
  /** The run was read again and its status is not the one it was given. */
  readonly updated = output<RunDetail>();
  readonly useSetup = output<ServedSetup>();

  private readonly record = signal<RunDetail | null>(null);
  private readonly readFailure = signal<string | null>(null);
  private generation = 0;
  protected readonly detail = this.record.asReadonly();
  protected readonly goldenReviewRun = signal<number | null>(null);
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

  protected readonly canUseSetup = computed(() => this.detail()?.setup != null && this.setupBlockedWhy() === null);
  /** Why the form cannot take the setup of this run just now, for a run that has one to take; null otherwise. */
  protected readonly setupWaitsWhy = computed(() => (this.detail()?.setup != null ? this.setupBlockedWhy() : null));
  /** Why a run that was read has no setup the page can open, in the server's words; null while the run has one or is not yet read. */
  protected readonly noSetupWhy = computed(() => {
    const detail = this.detail();
    if (detail === null || detail.setup != null) return null;
    return detail.setup_cannot_open_reason ?? 'The server did not send the setup this run used.';
  });

  private readonly heading = viewChild<ElementRef<HTMLElement>>('heading');

  constructor() {
    // A read that comes back after the panel is gone is not used, and starts no further poll.
    inject(DestroyRef).onDestroy(() => (this.generation += 1));
    // A run to show: read it, and start afresh if the run given is another one (the Backtest tab shows each new run in this panel).
    effect(() => {
      const id = this.runId();
      untracked(() => {
        this.cancelling.set(false);
        this.cancelProblem.set(null);
        this.readFailure.set(null);
        void this.load(id);
      });
    });
    // The job's stream says it is over before the next poll would find out: read the record at once.
    effect(() => {
      const status = this.job()?.status;
      if (status === 'completed' || status === 'failed' || status === 'cancelled') {
        untracked(() => {
          if (this.status() === 'running') void this.load(this.run().id);
        });
      }
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
      // Tell the page when the record says something other than it was given, so a finished run stops blocking a new start.
      if (detail.status !== this.run().status) this.updated.emit(detail);
    } catch (error) {
      if (generation !== this.generation) return;
      this.readFailure.set(error instanceof RunRefusedError ? error.message : extractServerMessage(error, 'The run could not be read.'));
    }
    this.lookAgainWhileGoing();
  }

  /** While the record says the run is going, whether or not the last read worked, read it again in a few seconds. */
  private lookAgainWhileGoing(): void {
    if (this.status() !== 'running') {
      this.poller.stop();
      return;
    }
    // The job's stream is not known to this tab (it began in another, or the first read of the jobs failed): ask for the jobs that are going again.
    if (this.job() === null) void this.jobs.refreshActive();
    this.poller.schedule(this.pollMs, () => void this.load(this.run().id));
  }

  protected async cancel(): Promise<void> {
    this.cancelProblem.set(null);
    // The button that was pressed is disabled once the cancel is asked for, and a disabled button takes no more keys: the keyboard moves to the run.
    if (this.host.nativeElement.contains(document.activeElement)) this.focusHeading();
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
}

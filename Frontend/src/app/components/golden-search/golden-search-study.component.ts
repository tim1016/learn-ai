import { ChangeDetectionStrategy, Component, computed, DestroyRef, effect, ElementRef, inject, input, output, signal, untracked, viewChild } from '@angular/core';
import { DecimalPipe } from '@angular/common';
import { RouterLink } from '@angular/router';
import { ButtonModule } from 'primeng/button';

import { ReceiptLabelPipe } from '../../shared/pipes/receipt-label.pipe';
import { ConfirmDeleteComponent } from '../../shared/research-record/confirm-delete.component';
import { RecordPoller } from '../../shared/research-record/record-poller';
import { extractServerMessage } from '../broker/operation-error';
import type { GridSearchRefusal } from '../grid-search/grid-search.types';
import { GoldenSearchReviewTour, reviewStepText, reviewStops, type ReviewStop } from './charts/golden-search-review-tour';
import { GoldenSearchWalkthroughComponent } from './charts/golden-search-walkthrough.component';
import { GoldenSearchCompareStepComponent } from './golden-search-compare-step.component';
import { GoldenSearchDecisionStepComponent } from './golden-search-decision-step.component';
import { GoldenSearchPlanSummaryComponent } from './golden-search-plan-summary.component';
import { GoldenSearchProcessComponent, researchStage } from './golden-search-process.component';
import { GoldenSearchScopeLineComponent } from './golden-search-scope-line.component';
import { GoldenSearchSearchStepComponent } from './golden-search-search-step.component';
import { primaryAction, singleStageAction, STUDY_STEPS, stepForState, stepProgress, type PrimaryAction, type StepProgress, type StudyStep } from './golden-search-steps';
import { GoldenSearchStudyStripComponent } from './golden-search-study-strip.component';
import { GoldenSearchTestStepComponent } from './golden-search-test-step.component';
import { GoldenSearchRefusedError, GoldenSearchService, StageDispatchError, StudyConflictError } from './golden-search.service';
import { isLive, type PresentedStatus, type StrategyCapability, type StudyCommand, type StudyCommandRequest, type StudyDetail } from './golden-search.types';
import { IdempotencyKeys } from './idempotency-keys';

/** Polls kept up after a dispatch while the worker has yet to claim the stage (its claim races the 202). */
const AWAIT_CLAIM_POLLS = 10;
/** Statuses after which Finish may resume the stage; the server's refusal explains when it cannot. */
const RESUMABLE_STATUSES: readonly PresentedStatus[] = ['failed', 'cancelled', 'interrupted'];

const PROGRESS_TEXT: Readonly<Record<StepProgress, string>> = {
  done: '(done)',
  current: '(waiting on you or running)',
  upcoming: '(not reached yet)',
};

/**
 * One Golden Search study (#2696): the study strip, the five steps, the
 * server's guidance for the current state with the one next action, the
 * scope line above every step, the
 * record controls (Cancel, Finish, Hide), and a footer naming the data and
 * where the final test and the current default stand. Review this study
 * walks the key charts across the steps in research order, one question at
 * each (#2821). Every stage change is a command
 * carrying the study's revision and an idempotency key; a command that
 * authorizes a stage starts its job through the jobs boundary. Polls while a
 * stage runs; a failed poll keeps the study shown and tries again, and an
 * answer older than the revision on screen is dropped.
 */
@Component({
  selector: 'app-golden-search-study',
  imports: [
    ButtonModule,
    ConfirmDeleteComponent,
    DecimalPipe,
    GoldenSearchCompareStepComponent,
    GoldenSearchDecisionStepComponent,
    GoldenSearchPlanSummaryComponent,
    GoldenSearchProcessComponent,
    GoldenSearchScopeLineComponent,
    GoldenSearchSearchStepComponent,
    GoldenSearchStudyStripComponent,
    GoldenSearchTestStepComponent,
    GoldenSearchWalkthroughComponent,
    ReceiptLabelPipe,
    RouterLink,
  ],
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './golden-search-study.component.html',
  styleUrl: './golden-search-study.component.scss',
  providers: [GoldenSearchReviewTour],
})
export class GoldenSearchStudyComponent {
  private readonly service = inject(GoldenSearchService);
  private readonly review = inject(GoldenSearchReviewTour);
  private readonly reviewButton = viewChild<ElementRef<HTMLButtonElement>>('reviewButton');
  private readonly destroyRef = inject(DestroyRef);

  readonly studyId = input.required<string>();
  readonly capabilities = input<readonly StrategyCapability[]>([]);
  /** Poll interval while a stage runs; tests set 0 to disable. */
  readonly pollMs = input(3000);
  readonly hidden = output<string>();

  readonly detail = signal<StudyDetail | null>(null);
  readonly loadError = signal<string | null>(null);
  /** A refresh of the study on screen failed; it stays shown (#2696). */
  readonly refreshNotice = signal<string | null>(null);
  readonly actionMessage = signal<string | null>(null);
  readonly actionRefusal = signal<GridSearchRefusal | null>(null);
  readonly busy = signal(false);
  readonly selectedStep = signal<StudyStep>('search');
  /** The server's guidance, announced once when the study moves to a new state (never on the first load or a poll that changes nothing). */
  readonly announcement = signal('');

  protected readonly progressText = PROGRESS_TEXT;
  /** The study review's stops, fixed when it opens; null when it is closed. */
  protected readonly reviewing = signal<readonly ReviewStop[] | null>(null);
  protected readonly reviewSteps = computed(() => (this.reviewing() ?? []).map((stop) => ({ text: reviewStepText(stop) })));
  protected readonly capability = computed(() => {
    const key = this.detail()?.strategy_key;
    return this.capabilities().find((c) => c.strategy_key === key) ?? null;
  });
  protected readonly steps = computed(() => {
    const state = this.detail()?.state;
    return STUDY_STEPS.map((step) => ({ ...step, progress: state === undefined ? 'upcoming' : stepProgress(step.id, state) }));
  });
  /** The next action, unless it only leads to the step already open. */
  protected readonly primary = computed<PrimaryAction | null>(() => {
    const detail = this.detail();
    const action = detail === null ? null : primaryAction(detail);
    return action?.kind === 'step' && action.step === this.selectedStep() ? null : action;
  });
  /** Run only the next stage and pause after it: the quieter alternative to Run research. */
  protected readonly secondary = computed<PrimaryAction | null>(() => {
    const detail = this.detail();
    return detail === null ? null : singleStageAction(detail);
  });
  protected readonly stage = computed(() => {
    const detail = this.detail();
    return detail === null ? null : researchStage(detail);
  });
  /** A command the server accepted whose job did not start; sending it again replays the same authorization. */
  protected readonly undispatched = signal<StudyCommandRequest | null>(null);
  protected readonly canCancel = computed(() => this.permitted('cancel'));
  protected readonly canFinish = computed(() => this.permitted('finish'));
  protected readonly finishRefusal = computed(() => {
    const detail = this.detail();
    return detail !== null && RESUMABLE_STATUSES.includes(detail.presented_status) ? (detail.action_refusals.finish ?? null) : null;
  });
  /**
   * The guidance detail, unless it is the failure reason of a failed
   * qualification: the Final decision step's proof-failure alert says that,
   * beside "Retry qualification".
   */
  protected readonly guidanceDetail = computed(() => {
    const detail = this.detail();
    if (detail === null) return null;
    return detail.state === 'qualification_failed' && detail.guidance.detail === detail.failure_reason ? null : detail.guidance.detail;
  });
  /** The study's failure reason, unless the guidance already says it or the proof-failure alert does. */
  protected readonly failureReason = computed(() => {
    const detail = this.detail();
    const reason = detail?.failure_reason ?? null;
    if (detail === null || reason === null || detail.state === 'qualification_failed' || detail.guidance.detail === reason) return null;
    return reason;
  });
  /** The footer's right-hand line: where the final test and the current default stand. */
  protected readonly footerState = computed(() => {
    const detail = this.detail();
    if (detail?.state === 'approved') return 'Golden settings ready · Deploy checks still apply';
    return detail?.exam_locked ? 'Final test opened once · Current default unchanged' : 'Final test held back · Current default unchanged';
  });
  /** Hiding a study whose stage still runs is refused by the server, so it is not offered. */
  protected readonly canHide = computed(() => {
    const detail = this.detail();
    return detail !== null && !detail.hidden && !isLive(detail.presented_status) && detail.state !== 'qualification_pending';
  });

  private readonly poller = new RecordPoller(this.destroyRef);
  private readonly keys = new IdempotencyKeys();
  /** Generation of the latest load; a poll that resolves late must not restore stale state. */
  private loadGeneration = 0;
  /** Which study the page shows; an answer about an earlier one must not reach this one. */
  private viewGeneration = 0;
  private stateStep: StudyStep | null = null;
  private awaitingClaimPolls = 0;

  constructor() {
    effect(() => {
      const id = this.studyId();
      untracked(() => {
        this.viewGeneration += 1;
        this.busy.set(false);
        this.clearFeedback();
        this.detail.set(null);
        this.refreshNotice.set(null);
        this.announcement.set('');
        this.stateStep = null;
        this.awaitingClaimPolls = 0;
        // A review belongs to the study it started on; another study starts without one.
        this.reviewing.set(null);
        this.review.stop.set(null);
        void this.reload(id);
      });
    });
  }

  async reload(id: string = this.studyId()): Promise<void> {
    const generation = ++this.loadGeneration;
    try {
      const detail = await this.service.get(id);
      if (generation !== this.loadGeneration) return;
      this.loadError.set(null);
      this.refreshNotice.set(null);
      this.apply(detail);
    } catch (error) {
      if (generation !== this.loadGeneration) return;
      const shown = this.detail();
      if (shown?.id !== id) {
        this.loadError.set(extractServerMessage(error, 'This study could not be loaded.'));
        return;
      }
      // One failed refresh of a study on screen is not the end of it.
      this.refreshNotice.set(this.schedulePoll(shown) ? 'This study could not be refreshed; retrying.' : 'This study could not be refreshed.');
    }
  }

  selectStep(step: StudyStep): void {
    this.selectedStep.set(step);
  }

  protected toggleReview(): void {
    const study = this.detail();
    if (this.reviewing() !== null) this.endReview();
    else if (study !== null) this.reviewing.set(reviewStops(study));
  }

  /** Opens the step that holds the stop's chart; its panel comes into view when it shows. */
  protected visitStop(index: number): void {
    const stop = this.reviewing()?.[index];
    if (stop === undefined) return;
    this.selectStep(stop.step);
    this.review.stop.set(stop);
  }

  protected endReview(): void {
    this.reviewing.set(null);
    this.review.stop.set(null);
    this.reviewButton()?.nativeElement.focus();
  }

  async runPrimary(action: PrimaryAction): Promise<void> {
    if (action.kind === 'step') this.selectStep(action.step);
    else await this.runCommand({ command: action.command, payload: {} });
  }

  async cancel(): Promise<void> {
    await this.runCommand({ command: 'cancel', payload: {} });
  }

  async finish(): Promise<void> {
    await this.runCommand({ command: 'finish', payload: {} });
  }

  async hide(): Promise<void> {
    const id = this.studyId();
    const view = this.viewGeneration;
    this.busy.set(true);
    this.clearFeedback();
    try {
      await this.service.hide(id);
      if (view !== this.viewGeneration) return;
      this.poller.stop();
      this.hidden.emit(id);
    } catch (error) {
      if (view !== this.viewGeneration) return;
      if (error instanceof GoldenSearchRefusedError) this.actionRefusal.set(error.refusal);
      else if (error instanceof StudyConflictError) this.actionMessage.set(error.message);
      else this.actionMessage.set(extractServerMessage(error, 'The study could not be hidden. Try again.'));
    } finally {
      if (view === this.viewGeneration) this.busy.set(false);
    }
  }

  /** A step's own action; locking a candidate moves on to the step where its final test is opened. */
  async onStepCommand(command: StudyCommand): Promise<void> {
    const accepted = await this.runCommand(command);
    if (accepted && command.command === 'select_candidate') this.selectStep('decision');
  }

  /**
   * Sends one command against the revision on screen; a retry after no answer
   * reuses its idempotency key. Resolves true when the server accepted it.
   */
  async runCommand(command: StudyCommand): Promise<boolean> {
    const detail = this.detail();
    if (detail === null || this.busy()) return false;
    const request: StudyCommandRequest = { ...command, expected_revision: detail.revision, idempotency_key: this.keys.keyFor(JSON.stringify([detail.id, detail.revision, command])) };
    return this.send(detail, request);
  }

  /** Sends the accepted command whose job did not start once more; its idempotency key replays the same dispatch. */
  async startAgain(): Promise<void> {
    const detail = this.detail();
    const request = this.undispatched();
    if (detail === null || request === null || this.busy()) return;
    await this.send(detail, request);
  }

  private async send(detail: StudyDetail, request: StudyCommandRequest): Promise<boolean> {
    const view = this.viewGeneration;
    this.busy.set(true);
    this.clearFeedback();
    try {
      const outcome = await this.service.command(detail.id, request);
      this.keys.settle();
      // The page moved to another study while this one's command was in flight: its answer is not this page's.
      if (view !== this.viewGeneration) return false;
      if (outcome.jobId !== null) this.awaitingClaimPolls = AWAIT_CLAIM_POLLS;
      this.applyAnswer(outcome.study);
      return true;
    } catch (error) {
      if (view !== this.viewGeneration) return false;
      this.onCommandFailure(error);
      if (error instanceof StageDispatchError) this.undispatched.set(request);
      return error instanceof StageDispatchError;
    } finally {
      if (view === this.viewGeneration) this.busy.set(false);
    }
  }

  private onCommandFailure(error: unknown): void {
    if (error instanceof StageDispatchError) {
      this.keys.settle();
      this.applyAnswer(error.study);
      this.actionMessage.set('The stage was authorized but its job did not start. Start it again: the retry reuses the same authorization.');
    } else if (error instanceof StudyConflictError) {
      this.keys.settle();
      if (error.current !== null) this.applyAnswer(error.current);
      this.actionMessage.set(
        error.code === 'STALE_REVISION' ? 'This study changed since it was shown (another tab, or a stage finished). Its current state is shown now — review it and try again.' : error.message,
      );
    } else if (error instanceof GoldenSearchRefusedError) {
      this.keys.settle();
      this.actionRefusal.set(error.refusal);
    } else {
      this.actionMessage.set(extractServerMessage(error, 'The command got no answer. Press it again: the retry is recognised and cannot act twice.'));
    }
  }

  private permitted(command: StudyCommand['command']): boolean {
    return this.detail()?.permitted_actions.includes(command) ?? false;
  }

  private clearFeedback(): void {
    this.actionMessage.set(null);
    this.actionRefusal.set(null);
    this.undispatched.set(null);
  }

  /** Shows the study a command answered with; a load still in flight was read before it and must not replace it. */
  private applyAnswer(detail: StudyDetail): void {
    this.loadGeneration += 1;
    this.apply(detail);
  }

  /** Shows a study; moves to the step holding its decision whenever that step changes. An answer older than the one shown is dropped. */
  private apply(detail: StudyDetail): void {
    if (detail.id !== this.studyId()) return;
    const previous = this.detail();
    if (previous !== null && previous.id === detail.id && detail.revision < previous.revision) {
      this.schedulePoll(previous);
      return;
    }
    if (previous !== null && previous.id === detail.id && previous.state !== detail.state) {
      this.announcement.set(`${detail.guidance.headline}. ${detail.guidance.detail}`);
    }
    this.detail.set(detail);
    const step = stepForState(detail.state);
    if (step !== this.stateStep) {
      this.stateStep = step;
      this.selectedStep.set(step);
    }
    if (isLive(detail.presented_status)) this.awaitingClaimPolls = 0;
    this.schedulePoll(detail);
  }

  /** Arms the next poll while the study's stage runs or its claim is awaited; true when one was armed. */
  private schedulePoll(detail: StudyDetail): boolean {
    if (isLive(detail.presented_status)) {
      this.poller.schedule(this.pollMs(), () => void this.reload());
      return true;
    }
    if (this.awaitingClaimPolls > 0) {
      this.awaitingClaimPolls -= 1;
      this.poller.schedule(this.pollMs(), () => void this.reload());
      return true;
    }
    this.poller.stop();
    return false;
  }
}

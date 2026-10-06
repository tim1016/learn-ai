import { ChangeDetectionStrategy, Component, computed, input, output, signal } from '@angular/core';
import { ButtonModule } from 'primeng/button';

import { ReceiptLabelPipe } from '../../shared/pipes/receipt-label.pipe';
import { TimestampDisplayComponent } from '../../shared/timestamp';
import { GoldenSearchApprovalComponent } from './golden-search-approval.component';
import { examScored, fitFor } from './golden-search-decision';
import { GoldenSearchExamResultComponent } from './golden-search-exam-result.component';
import { GoldenSearchFinalChartsComponent } from './golden-search-final-charts.component';
import { GoldenSearchLockboxComponent, type LockFit } from './golden-search-lockbox.component';
import { RETAIN_KINDS } from './golden-search-retain.component';
import type { StudyStep } from './golden-search-steps';
import { GoldenSearchTupleComponent } from './golden-search-tuple.component';
import type { CandidateKey, EvidenceCandidate, ExamFit, StrategyCapability, StudyCommand, StudyDetail, StudyState } from './golden-search.types';

type DecisionView = 'before' | 'lock' | 'running' | 'review' | 'finished';

const REVIEW_STATES: ReadonlySet<StudyState> = new Set(['awaiting_review', 'qualification_pending', 'approved', 'qualification_failed']);
const FINISHED_STATES: ReadonlySet<StudyState> = new Set(['retained', 'closed']);

/**
 * The Final decision step (#2696, #2930). Before the test: the lock — what
 * one look consumes and the acknowledgement that opens it for every fit.
 * While it runs: what is running. After it: every fit's result side by side,
 * then, for the fit the owner selects, its checks and charts, its exact
 * settings and the decision panel (approve that fit, keep current, or
 * another finish), then the golden settings and "Use in Deploy". Once an
 * approval names a fit the page stays on it. A finished study shows the
 * recorded decision.
 */
@Component({
  selector: 'app-golden-search-decision-step',
  imports: [
    ButtonModule,
    GoldenSearchApprovalComponent,
    GoldenSearchExamResultComponent,
    GoldenSearchFinalChartsComponent,
    GoldenSearchLockboxComponent,
    GoldenSearchTupleComponent,
    ReceiptLabelPipe,
    TimestampDisplayComponent,
  ],
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './golden-search-decision-step.component.html',
  styleUrl: './golden-search-decision-step.component.scss',
})
export class GoldenSearchDecisionStepComponent {
  readonly study = input.required<StudyDetail>();
  readonly capability = input<StrategyCapability | null>(null);
  readonly busy = input(false);
  readonly studyCommand = output<StudyCommand>();
  readonly goTo = output<StudyStep>();

  /** The fit the owner selected, for the study it was selected in. */
  private readonly selected = signal<{ readonly studyId: string; readonly key: CandidateKey } | null>(null);

  protected readonly view = computed<DecisionView>(() => {
    const study = this.study();
    if (FINISHED_STATES.has(study.state)) return 'finished';
    if (REVIEW_STATES.has(study.state) && examScored(study.results.exam)) return 'review';
    if (study.exam_locked) return 'running';
    return study.state === 'awaiting_candidate' && study.results.evidence !== null ? 'lock' : 'before';
  });
  private readonly candidates = computed(() => this.study().results.evidence?.candidates ?? []);
  /** The fits the final test will run, as the server lists them, each with its development evidence. */
  protected readonly fitsToTest = computed<LockFit[]>(() =>
    (this.study().results.evidence?.fits_to_test ?? []).flatMap((fit) => {
      const candidate = this.candidates().find((item) => item.key === fit.key);
      return candidate === undefined ? [] : [{ ...fit, candidate }];
    }),
  );
  /** The scored final test, or null before it has a result. */
  protected readonly exam = computed(() => {
    const exam = this.study().results.exam;
    return examScored(exam) ? exam : null;
  });
  /** The fit the page is about: the one an approval named, else the owner's selection, else the first. */
  protected readonly fit = computed<ExamFit | null>(() => {
    const exam = this.exam();
    if (exam === null) return null;
    const selected = this.selected();
    const key = exam.chosen_key ?? (selected?.studyId === this.study().id ? selected.key : null);
    return fitFor(exam, key) ?? exam.fits[0] ?? null;
  });
  protected readonly candidate = computed<EvidenceCandidate | null>(() => {
    const key = this.fit()?.key;
    return this.candidates().find((candidate) => candidate.key === key) ?? null;
  });
  protected readonly incumbent = computed(() => this.candidates().find((candidate) => candidate.key === 'incumbent') ?? null);
  protected readonly decisionLabel = computed(() => {
    const kind = this.study().decision?.kind;
    return RETAIN_KINDS.find((option) => option.kind === kind)?.label ?? null;
  });

  protected choose(key: CandidateKey): void {
    this.selected.set({ studyId: this.study().id, key });
  }
}

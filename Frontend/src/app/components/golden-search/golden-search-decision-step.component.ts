import { ChangeDetectionStrategy, Component, computed, input, output } from '@angular/core';
import { ButtonModule } from 'primeng/button';

import { ReceiptLabelPipe } from '../../shared/pipes/receipt-label.pipe';
import { TimestampDisplayComponent } from '../../shared/timestamp';
import { GoldenSearchApprovalComponent } from './golden-search-approval.component';
import { exposurePreview } from './golden-search-display';
import { GoldenSearchExamResultComponent } from './golden-search-exam-result.component';
import { GoldenSearchLockboxComponent } from './golden-search-lockbox.component';
import { RETAIN_KINDS } from './golden-search-retain.component';
import type { StudyStep } from './golden-search-steps';
import { GoldenSearchTupleComponent } from './golden-search-tuple.component';
import type { EvidenceCandidate, StrategyCapability, StudyCommand, StudyDetail, StudyState } from './golden-search.types';

type DecisionView = 'before' | 'lock' | 'running' | 'review' | 'finished';

const REVIEW_STATES: ReadonlySet<StudyState> = new Set(['awaiting_review', 'qualification_pending', 'approved', 'qualification_failed']);
const FINISHED_STATES: ReadonlySet<StudyState> = new Set(['retained', 'closed']);

/**
 * The Final decision step (#2696). Before the test: the lock — what one look
 * consumes and the acknowledgement that opens it. While it runs: what is
 * running. After it: the exact settings, the final-test result with its four
 * evidence rows, and the decision panel (approve, keep current, or another
 * finish), then the golden settings and "Use in Deploy". A finished study
 * shows the recorded decision.
 */
@Component({
  selector: 'app-golden-search-decision-step',
  imports: [
    ButtonModule,
    GoldenSearchApprovalComponent,
    GoldenSearchExamResultComponent,
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

  protected readonly view = computed<DecisionView>(() => {
    const study = this.study();
    if (FINISHED_STATES.has(study.state)) return 'finished';
    const outcome = study.results.exam?.outcome ?? null;
    if (REVIEW_STATES.has(study.state) && outcome !== null) return 'review';
    if (study.exam_locked) return 'running';
    return study.state === 'candidate_locked' ? 'lock' : 'before';
  });
  private readonly candidates = computed(() => this.study().results.evidence?.candidates ?? []);
  protected readonly candidate = computed<EvidenceCandidate | null>(() => {
    const key = this.study().results.exam?.candidate_key ?? this.study().candidate_key;
    return this.candidates().find((candidate) => candidate.key === key) ?? null;
  });
  protected readonly incumbent = computed(() => this.candidates().find((candidate) => candidate.key === 'incumbent') ?? null);
  /** The final interval's recorded use, shown before a candidate is locked so Compare's exposure row has evidence to point at. */
  protected readonly exposure = computed(() => exposurePreview(this.study()));
  protected readonly candidateLabel = computed(() => this.candidate()?.label ?? 'Locked candidate');
  protected readonly decisionLabel = computed(() => {
    const kind = this.study().decision?.kind;
    return RETAIN_KINDS.find((option) => option.kind === kind)?.label ?? null;
  });
}

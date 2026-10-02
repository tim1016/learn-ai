import { ChangeDetectionStrategy, Component, computed, effect, input, output, signal, untracked } from '@angular/core';
import { RouterLink } from '@angular/router';
import { ButtonModule } from 'primeng/button';

import { GOLDEN_DEPLOY_HANDOFF_ROUTE, GOLDEN_QUALIFICATION_QUERY_PARAM } from '../../fleet/account-workspace';
import { expectedDefault, factualNote } from './golden-search-decision';
import { GoldenSearchRetainComponent, RETAIN_KINDS, type RetainDecision } from './golden-search-retain.component';
import { isLive, type ExamView, type RetainKind, type StudyCommand, type StudyDetail } from './golden-search.types';

/**
 * The decision panel (#2696): a written reason, the acknowledgement that
 * independent engine agreement is missing (unchecked; it names the program
 * version), and — only when the server names a weakness of the final test
 * — a separate acknowledgement in the server's words. "Approve golden configuration" stays
 * disabled until all three hold. Keeping the current settings (or waiting,
 * or retaining as exploration) is always a complete decision, also when an
 * approval stopped before it published, whenever the server permits it.
 * Once approved, the recorded reason and "Use in Deploy".
 */
@Component({
  selector: 'app-golden-search-approval',
  imports: [ButtonModule, GoldenSearchRetainComponent, RouterLink],
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './golden-search-approval.component.html',
  styleUrl: './golden-search-approval.component.scss',
})
export class GoldenSearchApprovalComponent {
  readonly study = input.required<StudyDetail>();
  readonly exam = input.required<ExamView>();
  readonly busy = input(false);
  readonly studyCommand = output<StudyCommand>();

  readonly note = signal('');
  readonly parityAcknowledged = signal(false);
  readonly weaknessAcknowledged = signal(false);

  protected readonly deployRoute = GOLDEN_DEPLOY_HANDOFF_ROUTE;
  protected readonly retainKinds = RETAIN_KINDS.filter((option) => option.kind !== 'keep_current');
  protected readonly approved = computed(() => this.study().state === 'approved');
  protected readonly pending = computed(() => this.study().state === 'qualification_pending');
  /** The approval's stage still runs (or waits for its worker); a stopped one is pending but not live. */
  protected readonly building = computed(() => isLive(this.study().presented_status));
  protected readonly retry = computed(() => this.study().state === 'qualification_failed');
  protected readonly canApproveState = computed(() => this.study().permitted_actions.includes('approve'));
  protected readonly canRetain = computed(() => this.study().permitted_actions.includes('retain'));
  protected readonly weak = computed(() => this.exam().weakness.length > 0);
  protected readonly weaknessText = computed(() => this.exam().weakness.map((item) => item.text).join(' and '));
  protected readonly programVersion = computed(() => this.study().results.qualification?.deploy?.program_version ?? this.study().receipt.program_version ?? 'unversioned');
  protected readonly qualificationId = computed(() => this.study().results.qualification?.qualification_id ?? this.study().qualification_id);
  protected readonly deployQuery = computed(() => ({ [GOLDEN_QUALIFICATION_QUERY_PARAM]: this.qualificationId() }));
  protected readonly canApprove = computed(
    () => this.canApproveState() && !this.busy() && this.note().trim() !== '' && this.parityAcknowledged() && (!this.weak() || this.weaknessAcknowledged()),
  );
  /** What still stands between the owner and approval, in order. */
  protected readonly missing = computed(() => {
    if (!this.canApproveState()) return this.study().action_refusals.approve ?? null;
    if (this.note().trim() === '') return 'Write the reason for this decision.';
    if (!this.parityAcknowledged()) return 'Acknowledge the missing independent engine agreement.';
    if (this.weak() && !this.weaknessAcknowledged()) return 'Acknowledge the weak research evidence separately.';
    return null;
  });

  /** The study the form was prepared for; a poll of the same study keeps what the owner typed. */
  private preparedFor: string | null = null;

  constructor() {
    effect(() => {
      const study = this.study();
      const exam = this.exam();
      if (study.id === this.preparedFor || exam.outcome === null) return;
      this.preparedFor = study.id;
      untracked(() => {
        this.note.set(factualNote(exam));
        this.parityAcknowledged.set(false);
        this.weaknessAcknowledged.set(false);
      });
    });
  }

  protected onNote(event: Event): void {
    if (event.target instanceof HTMLTextAreaElement) this.note.set(event.target.value);
  }

  protected onParity(event: Event): void {
    if (event.target instanceof HTMLInputElement) this.parityAcknowledged.set(event.target.checked);
  }

  protected onWeakness(event: Event): void {
    if (event.target instanceof HTMLInputElement) this.weaknessAcknowledged.set(event.target.checked);
  }

  protected approve(): void {
    if (!this.canApprove()) return;
    this.studyCommand.emit({
      command: 'approve',
      payload: {
        note: this.note().trim(),
        acknowledge_missing_parity: true,
        acknowledge_research_weakness: this.weak() && this.weaknessAcknowledged(),
        expected_default_qualification_id: expectedDefault(this.study()),
      },
    });
  }

  protected retain(kind: RetainKind): void {
    this.studyCommand.emit({ command: 'retain', payload: { kind, note: this.note().trim() } });
  }

  protected retainDecision(decision: RetainDecision): void {
    this.studyCommand.emit({ command: 'retain', payload: { kind: decision.kind, note: decision.note } });
  }
}

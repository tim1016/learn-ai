import { ChangeDetectionStrategy, Component, computed, ElementRef, input, output, viewChild } from '@angular/core';
import { FormField, type FieldTree } from '@angular/forms/signals';
import { ButtonModule } from 'primeng/button';
import { InputText } from 'primeng/inputtext';

import { MOMENTUM_CHOICES, REENTRY_CHOICES, STOP_LOSS_CHOICES } from '../choices';
import { PillChoiceComponent } from '../controls/pill-choice.component';
import { SwitchComponent } from '../controls/switch.component';
import { contractLine } from '../leg-draft';
import type { ReentryKind } from '../options-backtest.types';
import type { LegDraft } from '../setup-draft';
import { legKey, NO_PROBLEMS, type FieldProblems, type LegField } from '../setup-problems';

/** The problems about a leg's contract, shown under its contract line. */
const CONTRACT_FIELDS: readonly LegField[] = ['leg', 'contracts', 'expiry', 'strike'];

/**
 * One leg: its contract line, copy and delete, and its three rules. A rule
 * that is switched off shows its fields greyed and sends nothing. Re-entering
 * on momentum is offered only while the leg has a momentum rule to ask again.
 */
@Component({
  selector: 'app-options-backtest-leg-card',
  imports: [FormField, InputText, ButtonModule, PillChoiceComponent, SwitchComponent],
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './leg-card.component.html',
  styleUrls: ['../options-backtest-card.scss', './leg-card.component.scss'],
})
export class LegCardComponent {
  readonly legForm = input.required<FieldTree<LegDraft, number>>();
  /** Zero-based; the card says "Leg 1" for 0. */
  readonly position = input.required<number>();
  readonly problems = input<FieldProblems>(NO_PROBLEMS);
  readonly canCopy = input(true);
  readonly copied = output();
  readonly removed = output();

  protected readonly stopLossChoices = STOP_LOSS_CHOICES;
  protected readonly momentumChoices = MOMENTUM_CHOICES;
  protected readonly reentryChoices = REENTRY_CHOICES;

  protected readonly leg = computed(() => this.legForm()().value());
  protected readonly name = computed(() => `Leg ${this.position() + 1}`);
  protected readonly line = computed(() => contractLine(this.leg()));
  protected readonly reasonId = computed(() => `ob-${this.leg().id}-reentry-reason`);
  protected readonly unavailableReentry = computed<readonly ReentryKind[]>(() => (this.leg().momentumOn ? [] : ['on_momentum']));
  protected readonly contractProblem = computed(() => this.problemsAt(CONTRACT_FIELDS));
  protected readonly stopLossProblem = computed(() => this.problemsAt(['stopLoss', 'stopLossValue']));
  protected readonly momentumProblem = computed(() => this.problemsAt(['momentum', 'momentumValue']));
  protected readonly reentryProblem = computed(() => this.problemsAt(['reentry', 'reentryCount']));
  /** The re-entry choice is described by why on momentum is unavailable, and by what is wrong with the re-entry. */
  protected readonly reentryKindDescribedBy = computed(() =>
    [this.leg().momentumOn ? null : this.reasonId(), this.reentryProblem() === null ? null : this.problemId('reentry')].flatMap((id) => id ?? []).join(' ') || null,
  );

  private readonly heading = viewChild.required('heading', { read: ElementRef<HTMLElement> });
  private readonly deleteButton = viewChild.required('deleteButton', { read: ElementRef<HTMLElement> });

  /** Puts the keyboard on this leg, for a screen reader to say which one it is. */
  focusHeading(): void {
    this.heading().nativeElement.focus();
  }

  /** Puts the keyboard on this leg's delete control, where the one deleted just before it stood. */
  focusDelete(): void {
    this.deleteButton().nativeElement.focus();
  }

  /** The id of the line that says what is wrong with one of the leg's parts. */
  protected problemId(part: 'contract' | 'stop' | 'momentum' | 'reentry'): string {
    return `ob-${this.leg().id}-${part}-problem`;
  }

  /** `aria-describedby` for a part's fields: its problem, when it has one. */
  protected describedByProblem(part: 'stop' | 'momentum' | 'reentry'): string | null {
    const message = part === 'stop' ? this.stopLossProblem() : part === 'momentum' ? this.momentumProblem() : this.reentryProblem();
    return message === null ? null : this.problemId(part);
  }

  protected problem(field: LegField): string | null {
    return this.problems().get(legKey(this.leg().id, field)) ?? null;
  }

  /** What is wrong at those fields, as one line; null when nothing is. */
  private problemsAt(fields: readonly LegField[]): string | null {
    const messages = fields.flatMap((field) => this.problem(field) ?? []);
    return messages.length === 0 ? null : messages.join(' ');
  }
}

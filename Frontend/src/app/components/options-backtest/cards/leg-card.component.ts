import { ChangeDetectionStrategy, Component, computed, input, output } from '@angular/core';
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
  protected readonly contractProblems = computed(() => this.problemsAt(CONTRACT_FIELDS));
  protected readonly stopLossProblems = computed(() => this.problemsAt(['stopLoss', 'stopLossValue']));
  protected readonly momentumProblems = computed(() => this.problemsAt(['momentum', 'momentumValue']));
  protected readonly reentryProblems = computed(() => this.problemsAt(['reentry', 'reentryCount']));

  protected problem(field: LegField): string | null {
    return this.problems().get(legKey(this.leg().id, field)) ?? null;
  }

  private problemsAt(fields: readonly LegField[]): string[] {
    return fields.flatMap((field) => this.problem(field) ?? []);
  }
}

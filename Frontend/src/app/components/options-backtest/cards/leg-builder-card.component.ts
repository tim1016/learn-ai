import { ChangeDetectionStrategy, Component, computed, ElementRef, input, output, viewChild } from '@angular/core';
import { FormField, type FieldTree } from '@angular/forms/signals';
import { ButtonModule } from 'primeng/button';
import { InputText } from 'primeng/inputtext';

import { EXPIRY_CHOICES, EXPIRY_HINTS, RIGHT_CHOICES, SIDE_CHOICES, STRIKE_CHOICES } from '../choices';
import { PillChoiceComponent } from '../controls/pill-choice.component';
import { builderProblems, type BuilderField, type LegBuilder } from '../leg-draft';
import type { SetupLimits } from '../options-backtest.types';
import { legsFull } from '../setup-limits';

/**
 * One row that reads left to right: Buy or Sell, call or put, contracts, expiry,
 * strike, and Add leg. A sold leg needs a bought leg of its kind to cover it, which
 * the server judges and a line under the row says. The added card keeps its
 * contract choices editable; the builder checks the initial choices here.
 */
@Component({
  selector: 'app-options-backtest-leg-builder-card',
  imports: [FormField, InputText, ButtonModule, PillChoiceComponent],
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './leg-builder-card.component.html',
  styleUrls: ['../options-backtest-card.scss', './leg-builder-card.component.scss'],
})
export class LegBuilderCardComponent {
  readonly form = input.required<FieldTree<LegBuilder>>();
  /** How many legs the setup holds now. */
  readonly legCount = input.required<number>();
  /** What the server allows; null while it is not known, and then the server judges. */
  readonly limits = input<SetupLimits | null>(null);
  readonly add = output();

  protected readonly sides = SIDE_CHOICES;
  protected readonly rights = RIGHT_CHOICES;
  protected readonly expiries = EXPIRY_CHOICES;
  protected readonly strikes = STRIKE_CHOICES;
  protected readonly maxLegs = computed(() => this.limits()?.max_legs ?? null);

  protected readonly builder = computed(() => this.form()().value());
  protected readonly problems = computed(() => builderProblems(this.builder(), this.limits()));
  protected readonly expiryHint = computed(() => EXPIRY_HINTS[this.builder().expiryKind]);
  protected readonly full = computed(() => legsFull(this.legCount(), this.limits()));
  protected readonly canAdd = computed(() => !this.full() && this.problems().size === 0);

  private readonly addButton = viewChild.required('addButton', { read: ElementRef<HTMLButtonElement> });
  private readonly contractsInput = viewChild.required('contracts', { read: ElementRef<HTMLInputElement> });

  /** Puts the keyboard on Add leg, or on the first field when something there must be put right before a leg can be added. */
  focusAdd(): void {
    const add = this.addButton().nativeElement;
    (add.disabled ? this.contractsInput().nativeElement : add).focus();
  }

  /** The id of the line that says what is wrong with one of the row's fields. */
  protected problemId(field: BuilderField): string {
    return `ob-builder-problem-${field}`;
  }

  /** `aria-describedby` for a field: its problem, when it has one. */
  protected describedBy(field: BuilderField): string | null {
    return this.problems().has(field) ? this.problemId(field) : null;
  }
}

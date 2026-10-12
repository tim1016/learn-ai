import { ChangeDetectionStrategy, Component, computed, input, output } from '@angular/core';
import { FormField, type FieldTree } from '@angular/forms/signals';
import { ButtonModule } from 'primeng/button';
import { InputText } from 'primeng/inputtext';

import { EXPIRY_CHOICES, RIGHT_CHOICES, STRIKE_CHOICES } from '../choices';
import { PillChoiceComponent } from '../controls/pill-choice.component';
import { builderProblems, MAX_LEGS, type LegBuilder } from '../leg-draft';

/**
 * One row that reads left to right: Buy, call or put, contracts, expiry,
 * strike, and Add leg. Only buying exists. A leg's contract cannot be edited
 * once it is added, so what is out of range is said here, before the leg exists.
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
  readonly add = output();

  protected readonly rights = RIGHT_CHOICES;
  protected readonly expiries = EXPIRY_CHOICES;
  protected readonly strikes = STRIKE_CHOICES;
  protected readonly maxLegs = MAX_LEGS;

  protected readonly builder = computed(() => this.form()().value());
  protected readonly problems = computed(() => builderProblems(this.builder()));
  protected readonly full = computed(() => this.legCount() >= MAX_LEGS);
  protected readonly canAdd = computed(() => !this.full() && this.problems().size === 0);
}

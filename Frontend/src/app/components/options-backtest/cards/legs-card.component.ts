import { ChangeDetectionStrategy, Component, computed, input, output } from '@angular/core';
import type { FieldTree } from '@angular/forms/signals';

import { MAX_LEGS } from '../leg-draft';
import type { SetupDraft } from '../setup-draft';
import { FIELD, NO_PROBLEMS, type FieldProblems } from '../setup-problems';
import { LegCardComponent } from './leg-card.component';

/** The legs of the setup, one numbered card each. Copy and delete are the owner's intent; the page carries them out. */
@Component({
  selector: 'app-options-backtest-legs-card',
  imports: [LegCardComponent],
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './legs-card.component.html',
  styleUrls: ['../options-backtest-card.scss', './legs-card.component.scss'],
})
export class LegsCardComponent {
  readonly form = input.required<FieldTree<SetupDraft>>();
  readonly problems = input<FieldProblems>(NO_PROBLEMS);
  readonly legCopied = output<string>();
  readonly legRemoved = output<string>();

  protected readonly maxLegs = MAX_LEGS;
  protected readonly legs = computed(() => this.form().legs().value());
  protected readonly full = computed(() => this.legs().length >= MAX_LEGS);
  /** With no legs the card already says to add one; the server's reason is shown once there are legs it can be about. */
  protected readonly legsProblem = computed(() => (this.legs().length === 0 ? null : (this.problems().get(FIELD.legs) ?? null)));
}

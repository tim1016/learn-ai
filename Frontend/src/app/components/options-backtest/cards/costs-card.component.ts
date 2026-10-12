import { ChangeDetectionStrategy, Component, input } from '@angular/core';
import { FormField, type FieldTree } from '@angular/forms/signals';
import { InputText } from 'primeng/inputtext';

import { FEE_CHOICES } from '../choices';
import { PillChoiceComponent } from '../controls/pill-choice.component';
import type { SetupDraft } from '../setup-draft';
import { FIELD, NO_PROBLEMS, type FieldProblems } from '../setup-problems';

/**
 * The cost assumptions a run is priced under: starting cash, slippage, and how
 * fees are charged. Numbers stay the text typed; the server judges their range.
 */
@Component({
  selector: 'app-options-backtest-costs-card',
  imports: [FormField, InputText, PillChoiceComponent],
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './costs-card.component.html',
  styleUrl: '../options-backtest-card.scss',
})
export class CostsCardComponent {
  readonly form = input.required<FieldTree<SetupDraft>>();
  readonly problems = input<FieldProblems>(NO_PROBLEMS);
  protected readonly field = FIELD;
  protected readonly feeChoices = FEE_CHOICES;
}

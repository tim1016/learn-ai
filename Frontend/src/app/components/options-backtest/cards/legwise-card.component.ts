import { ChangeDetectionStrategy, Component, input } from '@angular/core';
import { FormField, type FieldTree } from '@angular/forms/signals';

import { SQUARE_OFF_CHOICES } from '../choices';
import { PillChoiceComponent } from '../controls/pill-choice.component';
import type { SquareOff } from '../options-backtest.types';

/** Square off: whether one leg's stop closes that leg only, or every open leg. */
@Component({
  selector: 'app-options-backtest-legwise-card',
  imports: [FormField, PillChoiceComponent],
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './legwise-card.component.html',
  styleUrl: '../options-backtest-card.scss',
})
export class LegwiseCardComponent {
  readonly squareOff = input.required<FieldTree<SquareOff>>();
  readonly problem = input<string | null>(null);
  protected readonly choices = SQUARE_OFF_CHOICES;
}

import { ChangeDetectionStrategy, Component, input, model, output } from '@angular/core';
import type { FormValueControl } from '@angular/forms/signals';

import type { Choice } from '../choices';

let nextGroup = 0;

/**
 * An either-or as pills (the look of Golden Search's method choice): radios in
 * a fieldset, the chosen one filled. A signal-forms control: bind it with
 * `[formField]` and it takes the field's disabled state. A choice listed in
 * `unavailable` cannot be picked, but the chosen one is never held back, so a
 * choice that stopped being available still shows what the setup holds.
 */
@Component({
  selector: 'app-options-backtest-pill-choice',
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './pill-choice.component.html',
  styleUrl: './pill-choice.component.scss',
})
export class PillChoiceComponent<T extends string> implements FormValueControl<T> {
  readonly value = model.required<T>();
  readonly disabled = input(false);
  readonly touch = output();
  readonly options = input.required<readonly Choice<T>[]>();
  /** The group's accessible name. */
  readonly label = input.required<string>();
  readonly unavailable = input<readonly T[]>([]);
  /** The id of the text that explains the group, such as why a choice is unavailable. */
  readonly describedBy = input<string | null>(null);

  protected readonly group = `options-backtest-pills-${nextGroup++}`;

  protected isUnavailable(choice: T): boolean {
    return choice !== this.value() && this.unavailable().includes(choice);
  }
}

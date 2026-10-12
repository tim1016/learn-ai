import { ChangeDetectionStrategy, Component, input, model, output } from '@angular/core';
import type { FormCheckboxControl } from '@angular/forms/signals';

/** An on/off switch with its label. A signal-forms control: bind it with `[formField]`. */
@Component({
  selector: 'app-options-backtest-switch',
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './switch.component.html',
  styleUrl: './switch.component.scss',
})
export class SwitchComponent implements FormCheckboxControl {
  readonly checked = model.required<boolean>();
  readonly disabled = input(false);
  readonly touch = output();
  /** What is written beside the switch. */
  readonly label = input.required<string>();
  /** A fuller accessible name, when the label alone would not say which switch it is. */
  readonly accessibleName = input<string | null>(null);

  protected onChange(event: Event): void {
    if (event.target instanceof HTMLInputElement) this.checked.set(event.target.checked);
  }
}

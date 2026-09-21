import { ChangeDetectionStrategy, Component, input, output } from '@angular/core';

import { PickerOptionRowComponent } from '../../symbol-catalog/picker-option-row.component';
import type { PickerSymbol } from '../../symbol-catalog/symbol-catalog.types';

@Component({
  selector: 'app-instrument-option',
  imports: [PickerOptionRowComponent],
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './instrument-option.component.html',
  styleUrl: './instrument-option.component.scss',
})
export class InstrumentOptionComponent {
  readonly ticker = input.required<PickerSymbol>();
  readonly active = input(false);
  readonly picked = output<PickerSymbol>();

  select(event: MouseEvent): void {
    event.stopPropagation();
    this.picked.emit(this.ticker());
  }
}

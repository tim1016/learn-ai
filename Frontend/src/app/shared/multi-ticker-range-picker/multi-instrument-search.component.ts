import { ChangeDetectionStrategy, Component, computed, input, output, signal } from '@angular/core';

import { matchSymbols } from '../symbol-catalog/symbol-match';
import type { PickerSymbol } from '../symbol-catalog/symbol-catalog.types';
import { InstrumentOptionComponent } from '../ticker-range-picker/parts/instrument-option.component';

@Component({
  selector: 'app-multi-instrument-search',
  imports: [InstrumentOptionComponent],
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './multi-instrument-search.component.html',
  styleUrl: './multi-instrument-search.component.scss',
})
export class MultiInstrumentSearchComponent {
  readonly options = input.required<readonly PickerSymbol[]>();
  readonly selected = input.required<readonly string[]>();
  readonly picked = output<string>();
  readonly query = signal('');

  readonly addable = computed<readonly PickerSymbol[]>(() => {
    const selected = new Set(this.selected());
    const unselected = this.options().filter((ticker) => !selected.has(ticker.symbol));
    return matchSymbols(unselected, this.query()).slice(0, 8);
  });

  onQueryInput(event: Event): void {
    const target = event.target;
    if (target instanceof HTMLInputElement) this.query.set(target.value);
  }

  pick(symbol: string): void {
    this.query.set('');
    this.picked.emit(symbol);
  }
}

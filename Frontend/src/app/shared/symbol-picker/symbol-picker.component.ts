import { ChangeDetectionStrategy, Component, computed, input, model } from '@angular/core';

import { DEFAULT_ADJUSTMENT_MODE } from '../ticker-catalog';
import type { PriceAdjustmentMode } from '../data-lake';
import { InstrumentCardComponent } from '../ticker-range-picker/parts/instrument-card.component';
import type { TickerRange } from '../ticker-range-picker/ticker-range-picker.types';

/**
 * Symbol-only picker: the shared instrument card bound to a bare `string`.
 *
 * For hosts whose subject is one symbol and nothing else — an order leg, a
 * news filter, a live chain lookup — this is the whole integration: bind
 * `[(symbol)]`, name the tree the host's run reads through
 * `adjustmentMode`, and the card supplies the joined catalog, the coverage
 * badge and the ensure-coverage gate. The `TickerRange` projection pins
 * `from`/`to` to placeholders so the card's snap-to-held-window behavior is
 * a no-op here, exactly as `app-ticker-date-picker` does for its date
 * half: only the symbol crosses back.
 */
@Component({
  selector: 'app-symbol-picker',
  imports: [InstrumentCardComponent],
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `
    <app-instrument-card
      [appearance]="appearance()"
      [adjustmentMode]="adjustmentMode()"
      [value]="projection()"
      (valueChange)="onRangePatch($event)"
    />
  `,
})
export class SymbolPickerComponent {
  /** The selected symbol — empty string means nothing picked yet. */
  readonly symbol = model.required<string>();

  /** The lake tree this host's run reads; coverage and gating follow it. */
  readonly adjustmentMode = input<PriceAdjustmentMode>(DEFAULT_ADJUSTMENT_MODE);

  readonly appearance = input<'card' | 'flat'>('flat');

  protected readonly projection = computed<TickerRange>(() => ({
    symbol: this.symbol(),
    from: '',
    to: '',
    resolution: 'daily',
  }));

  protected onRangePatch(range: TickerRange): void {
    if (range.symbol !== this.symbol()) this.symbol.set(range.symbol);
  }
}

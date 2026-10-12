import { ChangeDetectionStrategy, Component, computed, input } from '@angular/core';
import type { FieldTree } from '@angular/forms/signals';

import { SymbolPickerComponent } from '../../../shared/symbol-picker/symbol-picker.component';

/**
 * The underlying the legs are bought on. The picker reads the raw price tree:
 * strike rules compare against raw prices, so a pick must never start a
 * backfill in the split-adjusted one.
 */
@Component({
  selector: 'app-options-backtest-instrument-card',
  imports: [SymbolPickerComponent],
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './instrument-card.component.html',
  styleUrl: '../options-backtest-card.scss',
})
export class InstrumentCardComponent {
  readonly underlying = input.required<FieldTree<string>>();
  /** What the server said about the underlying, if anything. */
  readonly problem = input<string | null>(null);

  protected readonly symbol = computed(() => this.underlying()().value());

  protected pick(symbol: string): void {
    this.underlying()().value.set(symbol);
  }
}

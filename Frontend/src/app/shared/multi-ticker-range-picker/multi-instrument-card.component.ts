import {
  ChangeDetectionStrategy,
  Component,
  computed,
  input,
  model,
  signal,
} from '@angular/core';
import { CommonModule } from '@angular/common';
import { FormsModule } from '@angular/forms';

import type { TickerOption } from '../ticker-range-picker/ticker-range-picker.types';
import type { MultiTickerRange } from './multi-ticker-range-picker.types';

/**
 * Multi-symbol Instrument card. Sibling-only — the canonical
 * single-symbol InstrumentCard's UX (cache hint, last-cached date,
 * snap-to-30-days-on-pick) doesn't generalize to a universe.
 *
 * Layout: chips for currently-selected symbols, "Add ticker" search
 * box, and "All / None" buttons in the header.
 */
@Component({
  selector: 'app-multi-instrument-card',
  imports: [CommonModule, FormsModule],
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './multi-instrument-card.component.html',
  styleUrls: ['./multi-instrument-card.component.scss'],
})
export class MultiInstrumentCardComponent {
  readonly value = model.required<MultiTickerRange>();
  readonly tickerPool = input<readonly TickerOption[]>([]);
  readonly recent = input<readonly string[]>([]);

  readonly query = signal('');

  readonly addable = computed<readonly TickerOption[]>(() => {
    const q = this.query().trim().toUpperCase();
    const selected = new Set(this.value().symbols);
    return this.tickerPool()
      .filter((t) => !selected.has(t.symbol))
      .filter(
        (t) =>
          !q || t.symbol.includes(q) || t.name.toUpperCase().includes(q),
      )
      .slice(0, 8);
  });

  add(symbol: string): void {
    const v = this.value();
    if (v.symbols.includes(symbol)) return;
    this.value.set({ ...v, symbols: [...v.symbols, symbol] });
    this.query.set('');
  }

  remove(symbol: string): void {
    const v = this.value();
    const next = v.symbols.filter((s) => s !== symbol);
    // Refuse to leave the universe empty — keep the last symbol so the
    // payload stays valid against MultiTickerRequest's min_length=1.
    this.value.set({ ...v, symbols: next.length === 0 ? v.symbols : next });
  }

  selectAll(): void {
    const all = this.tickerPool().map((t) => t.symbol);
    if (all.length === 0) return;
    this.value.set({ ...this.value(), symbols: all });
  }

  selectNone(): void {
    const pool = this.tickerPool();
    if (pool.length === 0) return;
    // Always keep at least the first pool symbol selected — see remove().
    this.value.set({ ...this.value(), symbols: [pool[0].symbol] });
  }
}

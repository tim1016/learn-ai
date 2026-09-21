import {
  ChangeDetectionStrategy,
  Component,
  computed,
  inject,
  input,
  model,
  signal,
  untracked,
} from '@angular/core';
import { CommonModule } from '@angular/common';
import { FormsModule } from '@angular/forms';

import type { TickerOption } from '../ticker-range-picker/ticker-range-picker.types';
import { DEFAULT_ADJUSTMENT_MODE, TickerCatalogService } from '../ticker-catalog';
import type { PriceAdjustmentMode } from '../data-lake';
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

  /** The lake tree this page's run will read. */
  readonly adjustmentMode = input<PriceAdjustmentMode>(DEFAULT_ADJUSTMENT_MODE);

  /**
   * A universe supplied by the host, for pickers whose membership is not the
   * lake — the Observatory's backfill panel offers the vendor's listing
   * catalog, because its whole job is adding symbols the lake doesn't hold
   * yet. `null` (the default) keeps the lake-universe behavior, including
   * its loading/unavailable states; a host universe renders verbatim and
   * owns its own status reporting.
   */
  readonly universe = input<readonly TickerOption[] | null>(null);

  private readonly catalog = inject(TickerCatalogService);
  private readonly view = computed(() => {
    // `viewFor` creates the mode's resource on first ask, and `resource()`
    // installs an effect — illegal inside a reactive context (NG0602). Track
    // the mode signal, then step outside tracking to build/fetch the view.
    const mode = this.adjustmentMode();
    return untracked(() => this.catalog.viewFor(mode));
  });
  readonly tickerPool = computed<readonly TickerOption[]>(() => {
    const hostUniverse = this.universe();
    return hostUniverse === null ? this.view().pool() : hostUniverse;
  });
  readonly catalogLoading = computed(() =>
    this.universe() === null ? this.view().loading() : false,
  );
  readonly catalogUnavailable = computed<string | null>(() =>
    this.universe() === null ? this.view().unavailable() : null,
  );
  /** A host universe is not the lake — the card's copy and guards follow. */
  readonly hostUniverse = computed(() => this.universe() !== null);

  /**
   * A universe of every instrument in the lake is a batch nobody meant to
   * launch. "All" used to mean three symbols; the lake grows, so the button
   * stops being a shortcut past some size and the operator picks explicitly.
   */
  readonly selectAllLimit = 12;
  readonly selectAllDisabled = computed(() => this.tickerPool().length > this.selectAllLimit);

  retryCatalog(): void {
    this.view().reload();
  }

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
    // Refuse to leave a lake universe empty — keep the last symbol so the
    // payload stays valid against MultiTickerRequest's min_length=1. A host
    // universe has no such invariant: an empty selection is an honest
    // "nothing picked yet", and the host's own submit gate refuses it.
    if (next.length === 0 && !this.hostUniverse()) return;
    this.value.set({ ...v, symbols: next });
  }

  selectAll(): void {
    if (this.selectAllDisabled()) return;
    const all = this.tickerPool().map((t) => t.symbol);
    if (all.length === 0) return;
    this.value.set({ ...this.value(), symbols: all });
  }

  selectNone(): void {
    if (this.hostUniverse()) {
      // "None" means none — over a vendor universe of thousands, keeping
      // pool[0] would quietly nominate an arbitrary symbol for backfill.
      this.value.set({ ...this.value(), symbols: [] });
      return;
    }
    const pool = this.tickerPool();
    if (pool.length === 0) return;
    // Always keep at least the first pool symbol selected — see remove().
    this.value.set({ ...this.value(), symbols: [pool[0].symbol] });
  }
}

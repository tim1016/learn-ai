import { ChangeDetectionStrategy, Component, computed, input, model, output } from '@angular/core';

import type { PickerSymbol } from '../symbol-catalog/symbol-catalog.types';
import { MultiInstrumentSearchComponent } from './multi-instrument-search.component';

/**
 * Multi-symbol selection primitive: chips for the current selection, an
 * "Add ticker" search box, and "All / None" actions.
 *
 * Presentation only — no catalog injection, no nullability mode switch.
 * The host adapts its catalog source (the joined picker universe, the
 * backfill panel's delisted policy) into the typed `options` input and the
 * `loading`/`unavailable`/`retry` status surface, and states its empty-
 * selection policy through `allowEmpty`. Suggestions render through the
 * shared `app-instrument-option`, so the list carries the same identity
 * icons, coverage badges and button semantics as every other picker.
 */
@Component({
  selector: 'app-multi-instrument-card',
  imports: [MultiInstrumentSearchComponent],
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './multi-instrument-card.component.html',
  styleUrls: ['./multi-instrument-card.component.scss'],
})
export class MultiInstrumentCardComponent {
  /** The selected symbols — the only thing this primitive owns. */
  readonly symbols = model.required<string[]>();

  /** The rows on offer, already adapted by the host. */
  readonly options = input.required<readonly PickerSymbol[]>();
  readonly loading = input(false);
  readonly unavailable = input<string | null>(null);
  /**
   * Whether "no selection" is a meaningful state. False keeps the lake
   * payloads' `min_length=1` invariant (the last symbol stays); true lets
   * the operator clear everything — the host's own submit gate refuses an
   * empty spec.
   */
  readonly allowEmpty = input(false);

  readonly retry = output();

  /**
   * A universe of every instrument on offer is a batch nobody meant to
   * launch. "All" used to mean three symbols; catalogs grow, so the button
   * stops being a shortcut past some size and the operator picks explicitly.
   */
  readonly selectAllLimit = 12;
  readonly selectAllDisabled = computed(() => this.options().length > this.selectAllLimit);
  readonly selectAllTitle = computed(() =>
    this.selectAllDisabled()
      ? `The catalog holds more than ${this.selectAllLimit} instruments — add them individually.`
      : null,
  );

  add(symbol: string): void {
    const selected = this.symbols();
    if (selected.includes(symbol)) return;
    this.symbols.set([...selected, symbol]);
  }

  remove(symbol: string): void {
    const next = this.symbols().filter((s) => s !== symbol);
    // See `allowEmpty`: an empty selection is either honest or refused.
    if (next.length === 0 && !this.allowEmpty()) return;
    this.symbols.set(next);
  }

  selectAll(): void {
    if (this.selectAllDisabled()) return;
    const all = this.options().map((t) => t.symbol);
    if (all.length === 0) return;
    this.symbols.set(all);
  }

  selectNone(): void {
    if (this.allowEmpty()) {
      // "None" means none — over a universe of thousands, keeping options[0]
      // would quietly nominate an arbitrary symbol.
      this.symbols.set([]);
      return;
    }
    const options = this.options();
    if (options.length === 0) return;
    // Always keep at least the first option selected — see remove().
    this.symbols.set([options[0].symbol]);
  }
}

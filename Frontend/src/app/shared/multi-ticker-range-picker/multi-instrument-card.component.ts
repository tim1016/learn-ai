import {
  ChangeDetectionStrategy,
  Component,
  computed,
  DestroyRef,
  effect,
  inject,
  input,
  model,
  output,
} from '@angular/core';

import { isHeldRow, type PickerSymbol } from '../symbol-catalog/symbol-catalog.types';
import { CoverageGateController } from '../symbol-catalog/coverage-gate.controller';
import { CoverageGateStripComponent } from '../symbol-catalog/coverage-gate-strip.component';
import type { PriceAdjustmentMode } from '../data-lake';
import { MultiInstrumentSearchComponent } from './multi-instrument-search.component';

/**
 * Multi-symbol selection primitive: chips for the current selection, an
 * "Add ticker" search box, and "All / None" actions.
 *
 * Presentation plus an optional coverage gate — no catalog injection, no
 * nullability mode switch. The host adapts its catalog source (the joined
 * picker universe, the backfill panel's delisted policy) into the typed
 * `options` input and the `loading`/`unavailable`/`degraded` status
 * surface, and states its empty-selection policy through `allowEmpty`.
 * Suggestions render through the shared `app-instrument-option`, so the
 * list carries the same identity icons, coverage badges and button
 * semantics as every other picker.
 *
 * `adjustmentMode` is the gate switch: `null` (the default) is a closed
 * list the host owns outright — picks apply immediately, which is what the
 * backfill panel needs, since it *is* the populate path. A mode means the
 * host's runs read that lake tree, so an unheld pick is gated on its
 * backfill (ADR 0066): the strip renders here, and the symbol joins the
 * selection only once the lake confirms the bars landed.
 */
@Component({
  selector: 'app-multi-instrument-card',
  imports: [MultiInstrumentSearchComponent, CoverageGateStripComponent],
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './multi-instrument-card.component.html',
  styleUrls: ['./multi-instrument-card.component.scss'],
})
export class MultiInstrumentCardComponent {
  /** The selected symbols — the only thing this primitive owns. */
  readonly symbols = model.required<string[]>();

  /**
   * The card's landmark name. Pages that mount more than one card (the
   * Observatory's query bar and backfill panel) must name each — AXE's
   * landmark-unique rule is about the operator being able to tell the
   * regions apart, not the rule itself.
   */
  readonly label = input('Instrument universe');

  /** The rows on offer, already adapted by the host. */
  readonly options = input.required<readonly PickerSymbol[]>();
  readonly loading = input(false);
  readonly unavailable = input<string | null>(null);
  /** The vendor catalog is dark but the lake answered — degraded, not empty. */
  readonly degraded = input<string | null>(null);
  /**
   * The lake tree the host's runs read. `null` — the default — keeps this
   * card a closed list whose picks apply immediately; a mode gates unheld
   * picks on their backfill into that tree.
   */
  readonly adjustmentMode = input<PriceAdjustmentMode | null>(null);
  /**
   * Whether "no selection" is a meaningful state. False keeps the lake
   * payloads' `min_length=1` invariant (the last symbol stays); true lets
   * the operator clear everything — the host's own submit gate refuses an
   * empty spec.
   */
  readonly allowEmpty = input(false);

  readonly retry = output();
  readonly retryVendor = output();

  /** This card's coverage gate — see {@link CoverageGateController}. */
  protected readonly gate = new CoverageGateController(() => this.adjustmentMode());

  constructor() {
    let sessionMode = this.adjustmentMode();
    effect(() => {
      const nextMode = this.adjustmentMode();
      if (nextMode !== sessionMode) {
        sessionMode = nextMode;
        this.gate.abandon();
      }
    });
    inject(DestroyRef).onDestroy(() => this.gate.abandon());
  }

  /**
   * A universe of every instrument on offer is a batch nobody meant to
   * launch. "All" used to mean three symbols; catalogs grow, so the button
   * stops being a shortcut past some size and the operator picks explicitly.
   * A gated card never offers "All" at any size: unheld symbols backfill
   * one at a time, and a bulk add would bypass the gate.
   */
  readonly selectAllLimit = 12;
  readonly selectAllDisabled = computed(
    () => this.adjustmentMode() !== null || this.options().length > this.selectAllLimit,
  );
  readonly selectAllTitle = computed(() => {
    if (this.adjustmentMode() !== null) {
      return 'Unheld symbols are backfilled one at a time — add them individually.';
    }
    return this.selectAllDisabled()
      ? `The catalog holds more than ${this.selectAllLimit} instruments — add them individually.`
      : null;
  });

  add(symbol: string): void {
    const mode = this.adjustmentMode();
    if (mode !== null) {
      const row = this.options().find((t) => t.symbol === symbol);
      if (row === undefined || !isHeldRow(row)) {
        // The same admission the single-symbol card makes: refuse while
        // the lake's verdict is unknowable, refuse a tree nothing can
        // backfill, else gate the pick on its backfill.
        this.gate.admit({
          symbol,
          mode,
          lakeDark: () => this.unavailable(),
          commit: (covered) => this.commitAdd(covered),
        });
        return;
      }
    }
    this.commitAdd(symbol);
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

  protected retryGate(): void {
    this.gate.retry();
  }

  /** Cancel and Dismiss are the same act: this card lets its gate go. */
  protected closeGate(): void {
    this.gate.abandon();
  }

  private commitAdd(symbol: string): void {
    const selected = this.symbols();
    if (selected.includes(symbol)) return;
    this.symbols.set([...selected, symbol]);
  }
}

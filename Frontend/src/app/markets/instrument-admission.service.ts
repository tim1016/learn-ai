import { DestroyRef, Injectable, computed, effect, inject, signal, untracked } from '@angular/core';

import type { CoverageGateState } from '../shared/symbol-catalog/ensure-coverage.service';
import { CoverageGateController } from '../shared/symbol-catalog/coverage-gate.controller';
import { SymbolCatalogService } from '../shared/symbol-catalog/symbol-catalog.service';
import { isHeldRow, type PickerSymbol } from '../shared/symbol-catalog/symbol-catalog.types';
import { isUsEquitySymbol } from '../shared/symbol-catalog/us-equity-symbol';
import { DEFAULT_ADJUSTMENT_MODE } from '../shared/ticker-catalog';
import { InstrumentContextService } from './instrument-context.service';

/**
 * The lake tree that decides whether the lake holds an instrument at all, and so the tree its gate fills. It is the
 * tree the chart reads by default; the Data tab's own Fill repairs the other tree's gaps.
 */
const ADMISSION_MODE = DEFAULT_ADJUSTMENT_MODE;

/** Whether an instrument's place can open, and if not, why (PRD #3490 R07: the one shared gate, no private one). */
export type InstrumentAdmission =
  | { readonly kind: 'checking' }
  /** The address names something the lake cannot hold as an instrument. */
  | { readonly kind: 'not-a-symbol' }
  /** The catalog answered: the lake does not hold it and the vendor does not list it. */
  | { readonly kind: 'unlisted' }
  /** The catalog could not answer, so nothing is known about the symbol. */
  | { readonly kind: 'unavailable'; readonly reason: string }
  /** Listed, held nowhere, and nothing is filling it: nobody has asked for a fill, or it was dismissed. */
  | { readonly kind: 'unheld' }
  | { readonly kind: 'filling'; readonly gate: CoverageGateState }
  | { readonly kind: 'fill-failed'; readonly gate: CoverageGateState }
  | { readonly kind: 'ready'; readonly row: PickerSymbol };

/**
 * Opens an instrument's place only for an instrument the lake holds.
 *
 * A held symbol opens at once. A listed symbol the lake holds nothing of lands
 * on `unheld` and starts nothing: opening an address (a bookmark, Back, a
 * reload, an old link) never submits a backfill. The person's click on `fill()`
 * sends it through the same coverage gate the picker uses (`CoverageGateController`
 * over `EnsureCoverageService`, ADR 0066): the fill is shown, and the place opens
 * when the lake itself confirms the bars. The pickers (Markets search, the
 * context bar) gate before they navigate, so they arrive on a held symbol. A
 * symbol nothing lists never opens, and nothing opens in its place.
 *
 * The gate belongs to the address's symbol, and to nothing else. Moving to
 * another symbol, Dismiss and destroy release this place's hold on the fill
 * (the shared service stops the job only when nobody else waits on it), and a
 * completion that arrives for the old symbol cannot open it. The catalog
 * re-reading is not a reason to release it: the gate itself re-reads the
 * catalog when a job ends, and a fill that landed no bars must stay on screen
 * as the failure it is.
 */
@Injectable()
export class InstrumentAdmissionService {
  private readonly context = inject(InstrumentContextService);
  private readonly view = untracked(() => inject(SymbolCatalogService).viewFor(ADMISSION_MODE));
  private readonly gate = new CoverageGateController(() => ADMISSION_MODE);

  /** Symbols the lake confirmed after a fill, until the catalog's own read shows them held. */
  private readonly covered = signal<ReadonlySet<string>>(new Set());

  readonly state = computed<InstrumentAdmission>(() => {
    const symbol = this.context.symbol();
    if (!isUsEquitySymbol(symbol)) return { kind: 'not-a-symbol' };
    const row = this.view.pool().find((candidate) => candidate.symbol === symbol);
    if (row !== undefined && isHeldRow(row)) return { kind: 'ready', row };
    if (this.covered().has(symbol)) {
      return { kind: 'ready', row: row ?? { symbol, name: symbol, firstHeld: null, lastHeld: null, delisted: false } };
    }
    const gate = this.gate.state();
    if (gate !== null && gate.symbol === symbol) {
      return gate.phase === 'backfilling' ? { kind: 'filling', gate } : { kind: 'fill-failed', gate };
    }
    const status = this.view.status();
    if (status.kind === 'loading') return { kind: 'checking' };
    if (status.kind === 'unavailable') return { kind: 'unavailable', reason: status.message };
    if (row === undefined) {
      return status.kind === 'degraded' ? { kind: 'unavailable', reason: status.message } : { kind: 'unlisted' };
    }
    return { kind: 'unheld' };
  });

  constructor() {
    // The fill is the address's symbol's: moving to another one lets go of the old fill.
    effect(() => {
      this.context.symbol();
      untracked(() => this.gate.abandon());
    });
    inject(DestroyRef).onDestroy(() => this.gate.abandon());
  }

  /** Why no coverage verdict exists right now, or `null` when the lake answered. */
  private lakeDark(): string | null {
    const status = this.view.status();
    if (status.kind === 'unavailable') return status.message;
    return status.kind === 'loading' ? 'The lake coverage read is still in flight.' : null;
  }

  /**
   * Fills the instrument the address names: the person's explicit action on a
   * listed symbol the lake holds nothing of. It is the only way this place
   * starts a fill, and it does nothing in any other state (a fill already
   * running or failed is asked for again with `retry()`).
   */
  fill(): void {
    if (this.state().kind !== 'unheld') return;
    this.gate.admit({
      symbol: this.context.symbol(),
      mode: ADMISSION_MODE,
      lakeDark: () => this.lakeDark(),
      commit: (confirmed) => this.covered.update((held) => new Set(held).add(confirmed)),
    });
  }

  /** Re-decides the pending fill against the lake as it stands now. */
  retry(): void {
    this.gate.retry();
  }

  /** Lets this place's fill go; the shared gate stops the job only when no other caller waits on it. */
  dismiss(): void {
    this.gate.abandon();
  }

  /** Reads the lake's coverage and the listing again, for a catalog that could not answer. */
  retryCatalog(): void {
    this.view.reload();
    this.view.retryVendor();
  }
}

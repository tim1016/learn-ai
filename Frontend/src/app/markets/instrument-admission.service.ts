import { DestroyRef, Injectable, computed, effect, inject, signal, untracked } from '@angular/core';

import type { CoverageGateState } from '../shared/symbol-catalog/ensure-coverage.service';
import { CoverageGateController } from '../shared/symbol-catalog/coverage-gate.controller';
import { SymbolCatalogService } from '../shared/symbol-catalog/symbol-catalog.service';
import { isHeldRow, type PickerSymbol } from '../shared/symbol-catalog/symbol-catalog.types';
import { isUsEquitySymbol } from '../shared/symbol-catalog/us-equity-symbol';
import { DEFAULT_ADJUSTMENT_MODE } from '../shared/ticker-catalog';
import { InstrumentContextService } from './instrument-context.service';

/** The lake tree the place's chart reads, and so the tree its gate fills. */
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
  /** Listed, held nowhere, and nothing is filling it: the fill was dismissed. */
  | { readonly kind: 'unheld' }
  | { readonly kind: 'filling'; readonly gate: CoverageGateState }
  | { readonly kind: 'fill-failed'; readonly gate: CoverageGateState }
  | { readonly kind: 'ready'; readonly row: PickerSymbol };

/**
 * Opens an instrument's place only for an instrument the lake holds.
 *
 * A held symbol opens at once. A listed symbol the lake holds nothing of goes
 * through the same coverage gate the picker uses (`CoverageGateController` over
 * `EnsureCoverageService`, ADR 0066): the fill is shown, and the place opens
 * when the lake itself confirms the bars. Landing on the address is the pick.
 * A symbol nothing lists never opens, and nothing opens in its place.
 *
 * The gate belongs to the address's current symbol. Moving to another symbol
 * releases this place's hold on the old fill (the shared service stops the job
 * only when nobody else waits on it), and a completion that arrives for the
 * old symbol cannot open it.
 */
@Injectable()
export class InstrumentAdmissionService {
  private readonly context = inject(InstrumentContextService);
  private readonly view = untracked(() => inject(SymbolCatalogService).viewFor(ADMISSION_MODE));
  private readonly gate = new CoverageGateController(() => ADMISSION_MODE);

  /** Symbols the lake confirmed after a fill, until the catalog's own read shows them held. */
  private readonly covered = signal<ReadonlySet<string>>(new Set());
  /** The symbol this place has asked the gate to fill; keeps one fill per symbol. */
  private admitted: string | null = null;

  /** The listed symbol the lake holds nothing of, when the catalog is settled enough to say so. */
  private readonly needsFill = computed<string | null>(() => {
    const symbol = this.context.symbol();
    if (!isUsEquitySymbol(symbol) || this.covered().has(symbol)) return null;
    const row = this.view.pool().find((candidate) => candidate.symbol === symbol);
    if (row === undefined || isHeldRow(row)) return null;
    return this.view.status().kind === 'ready' ? symbol : null;
  });

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
    effect(() => {
      const target = this.needsFill();
      untracked(() => {
        if (target === null) {
          this.gate.abandon();
          this.admitted = null;
          return;
        }
        if (this.admitted !== target) this.admit(target);
      });
    });
    inject(DestroyRef).onDestroy(() => this.gate.abandon());
  }

  private admit(symbol: string): void {
    this.admitted = symbol;
    this.gate.admit({
      symbol,
      mode: ADMISSION_MODE,
      lakeDark: () => this.lakeDark(),
      commit: (confirmed) => this.covered.update((held) => new Set(held).add(confirmed)),
    });
  }

  /** Why no coverage verdict exists right now, or `null` when the lake answered. */
  private lakeDark(): string | null {
    const status = this.view.status();
    if (status.kind === 'unavailable') return status.message;
    return status.kind === 'loading' ? 'The lake coverage read is still in flight.' : null;
  }

  /** The fill the person dismissed, asked for again. */
  fill(): void {
    const target = this.needsFill();
    if (target !== null) this.admit(target);
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

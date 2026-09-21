import {
  ChangeDetectionStrategy,
  Component,
  computed,
  DestroyRef,
  effect,
  ElementRef,
  inject,
  input,
  model,
  signal,
  untracked,
  viewChild,
} from '@angular/core';
import { Tooltip } from 'primeng/tooltip';

import { type TickerOption, type TickerRange } from '../ticker-range-picker.types';
import { DEFAULT_ADJUSTMENT_MODE } from '../../ticker-catalog';
import { SymbolCatalogService } from '../../symbol-catalog/symbol-catalog.service';
import type { SymbolCatalogStatus } from '../../symbol-catalog/symbol-catalog.service';
import { CoverageGateController } from '../../symbol-catalog/coverage-gate.controller';
import { isHeldRow } from '../../symbol-catalog/symbol-catalog.types';
import {
  InstrumentDropdownComponent,
  type InstrumentDropdownView,
} from './instrument-dropdown.component';
import { InstrumentHeldSpanComponent } from './instrument-held-span.component';
import type { PickerSymbol } from '../../symbol-catalog/symbol-catalog.types';
import { toPickerSymbol } from '../../symbol-catalog/symbol-catalog.types';
import type { PriceAdjustmentMode } from '../../data-lake';
import { AssetIdentityComponent } from '../../asset-identity';
import { toMostRecentTradingDayIso } from '../../date/weekday';

/**
 * Does the window on screen intersect the days the lake holds for `t`?
 *
 * Both sides are zero-padded `YYYY-MM-DD`, so lexicographic order is
 * chronological order and no `Date` round-trip is needed. An unknown bound is
 * treated as open, which keeps an unlabelled span from moving the window.
 */
function overlapsHeldRange(window: TickerRange, t: TickerOption): boolean {
  const first = t.firstHeld ?? null;
  const last = t.lastHeld ?? null;
  if (last !== null && window.from > last) return false;
  if (first !== null && window.to < first) return false;
  return true;
}

const EXCHANGE_NAMES: Readonly<Record<string, string>> = {
  ARCA: 'NYSE Arca',
  NASDAQ: 'NASDAQ',
  NYSE: 'New York Stock Exchange',
  BATS: 'Cboe BZX',
  IEX: 'IEX',
  AMEX: 'NYSE American',
};

@Component({
  selector: 'app-instrument-card',
  imports: [
    Tooltip,
    AssetIdentityComponent,
    InstrumentDropdownComponent,
    InstrumentHeldSpanComponent,
  ],
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './instrument-card.component.html',
  styleUrls: ['./instrument-card.component.scss'],
  host: {
    '(document:mousedown)': 'onDocumentMouseDown($event)',
  },
})
export class InstrumentCardComponent {
  /** Rows rendered per dropdown open — past this, search is the scaler. */
  private static readonly MAX_VISIBLE_ROWS = 50;

  /**
   * This card's listbox id. Pages can mount several cards (an order entry
   * with N legs), and every combobox must reference its own listbox — a
   * shared literal id breaks the association and AXE's duplicate-id check.
   */
  private static nextListboxId = 0;
  readonly listboxId = `ticker-range-picker-listbox-${InstrumentCardComponent.nextListboxId++}`;

  readonly value = model.required<TickerRange>();
  readonly appearance = input<'card' | 'flat'>('card');
  /**
   * The card's landmark and combobox name. Pages that mount several cards
   * (an order entry with N legs) must name each — AXE's landmark-unique
   * rule is about the operator telling the pickers apart.
   */
  readonly label = input('Instrument');

  /**
   * The tree this picker's run will read. Belongs to the host page — Strategy
   * Lab sends `adjusted: false` for a `both` run, Data Lab has its own
   * toggle — because coverage differs between trees.
   */
  readonly adjustmentMode = input<PriceAdjustmentMode>(DEFAULT_ADJUSTMENT_MODE);

  /**
   * A universe supplied by the host, for pickers that own their membership
   * outright. A host universe is a closed list with no gate: the card
   * renders it verbatim. `null` — the default — means the joined catalog:
   * every listed symbol offered, lake coverage badged, and an unheld pick
   * gated on its backfill.
   */
  readonly universe = input<readonly TickerOption[] | null>(null);

  private readonly symbols = inject(SymbolCatalogService);
  private readonly destroyRef = inject(DestroyRef);
  /** This card's coverage gate — see {@link CoverageGateController}. */
  readonly gate = new CoverageGateController(() => this.adjustmentMode());
  private readonly view = computed(() => {
    // `viewFor` creates the mode's resource on first ask, and `resource()`
    // installs an effect — illegal inside a reactive context (NG0602). Track
    // the mode signal, then step outside tracking to build/fetch the view.
    const mode = this.adjustmentMode();
    return untracked(() => this.symbols.viewFor(mode));
  });

  readonly tickerPool = computed<readonly PickerSymbol[]>(() => {
    const hostUniverse = this.universe();
    return hostUniverse === null ? this.view().pool() : hostUniverse.map(toPickerSymbol);
  });
  readonly recent = computed<readonly string[]>(() =>
    this.universe() === null ? this.view().recent() : [],
  );
  readonly catalogStatus = computed<SymbolCatalogStatus>(() =>
    this.universe() === null ? this.view().status() : { kind: 'ready' },
  );
  /**
   * Why no coverage verdict exists right now — the lake's failure, or its
   * read still being in flight. A gate may not run on either: while the
   * lake loads, vendor-only rows already render with no held verdict, and
   * gating one would be a full-history backfill on a guess.
   */
  readonly coverageUnknown = computed<string | null>(() => {
    if (this.universe() !== null) return null;
    const status = this.view().status();
    if (status.kind === 'unavailable') return status.message;
    if (status.kind === 'loading') return 'The lake coverage read is still in flight.';
    return null;
  });
  /** A host-supplied universe is not the joined catalog, so its copy differs. */
  readonly hostUniverse = computed(() => this.universe() !== null);

  private readonly rootEl = viewChild.required<ElementRef<HTMLElement>>('rootEl');
  private readonly searchInput = viewChild<ElementRef<HTMLInputElement>>('searchInput');

  readonly open = signal(false);
  readonly query = signal('');

  constructor() {
    let sessionMode = this.adjustmentMode();
    effect(() => {
      const nextMode = this.adjustmentMode();
      if (nextMode !== sessionMode) {
        sessionMode = nextMode;
        this.gate.abandon();
      }
    });
    effect(() => {
      if (this.open()) {
        const input = this.searchInput();
        if (input) queueMicrotask(() => input.nativeElement.focus());
      }
    });
    // A symbol that changed without this card picking it — a host seed, a
    // strategy switch, a reset — voids the pending pick exactly as a fresh
    // pick does; a finished backfill must never overwrite the newer symbol.
    let lastSymbol: string | null = null;
    effect(() => {
      const symbol = this.value().symbol;
      if (lastSymbol !== null && symbol !== lastSymbol) {
        this.gate.abandon();
      }
      lastSymbol = symbol;
    });
    this.destroyRef.onDestroy(() => this.gate.abandon());
  }

  readonly selectedTicker = computed<PickerSymbol | undefined>(() =>
    this.tickerPool().find((t) => t.symbol === this.value().symbol),
  );

  readonly selectedFirstHeld = computed<string | null>(
    () => this.selectedTicker()?.firstHeld ?? null,
  );

  readonly selectedLastHeld = computed<string | null>(
    () => this.selectedTicker()?.lastHeld ?? null,
  );

  readonly selectedExchange = computed(() => this.selectedTicker()?.exchange ?? '—');

  readonly selectedExchangeTooltip = computed<string>(() => {
    const code = this.selectedExchange();
    const symbol = this.value().symbol;
    const name = EXCHANGE_NAMES[code];
    if (!name) {
      return 'Listing exchange — where this instrument is primarily traded.';
    }
    return `${name} — primary listing venue for ${symbol}.`;
  });

  readonly filteredTickers = computed<readonly PickerSymbol[]>(() => {
    const q = this.query().trim().toUpperCase();
    const pool = this.tickerPool();
    if (!q) return pool;
    return pool.filter((t) => t.symbol.includes(q) || t.name.toUpperCase().includes(q));
  });

  /**
   * The rows the dropdown actually renders. The joined universe runs to
   * ~11k vendor symbols; rendering all of them (each an identity component)
   * on every host is the difference between a dropdown and a freeze. The
   * match count stays honest — the header reads `filteredTickers()` — and
   * the search box is how an operator reaches anything past the cap.
   */
  readonly visibleTickers = computed<readonly PickerSymbol[]>(() =>
    this.filteredTickers().slice(0, InstrumentCardComponent.MAX_VISIBLE_ROWS),
  );

  readonly recentTickers = computed<readonly PickerSymbol[]>(() => {
    const recent = this.recent();
    if (recent.length === 0) return [];
    const pool = this.tickerPool();
    return recent
      .map((s) => pool.find((t) => t.symbol === s))
      .filter((t): t is PickerSymbol => !!t);
  });

  readonly dropdownView = computed<InstrumentDropdownView>(() => ({
    visible: this.visibleTickers(),
    recent: this.recentTickers(),
    matchCount: this.filteredTickers().length,
    activeSymbol: this.value().symbol,
    query: this.query(),
    catalogStatus: this.catalogStatus(),
    hostUniverse: this.hostUniverse(),
  }));

  /** The lake's own read failed — retry the lake, not the vendor. */
  retryCatalog(): void {
    this.view().reload();
  }

  /** The live catalog is dark but the lake answered — retry the vendor. */
  retryVendorCatalog(): void {
    this.view().retryVendor();
  }

  trackBySymbol(_: number, t: PickerSymbol): string {
    return t.symbol;
  }

  openDropdown(): void {
    if (this.open()) return;
    this.open.set(true);
    this.query.set('');
    // Re-read on open so a symbol backfilled since this tab loaded is
    // selectable without a page reload. Only the lake's coverage is
    // refreshed — the vendor catalog is read once per tab and re-fetched
    // solely through the degraded banner's Retry, which is a statement
    // that the last vendor answer was bad. `resource.reload()` is a no-op
    // while one is already in flight, so opening repeatedly costs at most
    // one read.
    if (this.universe() === null) this.view().reload();
  }

  closeDropdown(): void {
    this.open.set(false);
  }

  onTickerBoxEnter(event: Event): void {
    if (!this.open()) {
      this.openDropdown();
      event.preventDefault();
    }
  }

  onTickerBoxSpace(event: Event): void {
    if (!this.open()) {
      this.openDropdown();
      event.preventDefault();
    }
  }

  onDocumentMouseDown(event: MouseEvent): void {
    const host = this.rootEl().nativeElement;
    if (!host.contains(event.target as Node)) {
      this.closeDropdown();
    }
  }

  onSearchInput(value: string): void {
    this.query.set(value);
  }

  onSearchInputEvent(event: Event): void {
    const target = event.target;
    if (target instanceof HTMLInputElement) this.onSearchInput(target.value);
  }

  // Not `async` on purpose: the held path is synchronous so existing callers
  // keep their timing, and only the gate path awaits the coverage service.
  pickTicker(t: TickerOption): void {
    // A host universe owns membership outright, and a held symbol is
    // already runnable — delisted-but-held included; its bars are real.
    // Both pick exactly as the picker always has.
    if (this.universe() !== null || isHeldRow(t)) {
      this.applyPick(t);
      return;
    }
    // An unheld pick is the gate's to decide: refuse while the lake's
    // verdict is unknowable, refuse a tree nothing can backfill, else
    // backfill and commit only on the lake's own confirmation.
    this.gate.admit({
      symbol: t.symbol,
      mode: this.adjustmentMode(),
      lakeDark: () => this.coverageUnknown(),
      commit: (covered) => this.onCovered(covered),
    });
  }

  protected retryGate(): void {
    this.gate.retry();
  }

  /** Cancel and Dismiss are the same act: this card lets its gate go. */
  protected async closeGate(): Promise<void> {
    this.gate.abandon();
    this.refocusSearch();
  }

  /** The lake confirmed the bars — select the fresh row (or a bare symbol). */
  private onCovered(symbol: string): void {
    const fresh = this.tickerPool().find((p) => p.symbol === symbol);
    this.applyPick(fresh ?? { symbol, name: symbol });
  }

  /** The gate strip's buttons unmount on dismissal; keep focus in the box. */
  private refocusSearch(): void {
    if (!this.open()) return;
    queueMicrotask(() => this.searchInput()?.nativeElement.focus());
  }

  private applyPick(t: TickerOption): void {
    this.gate.abandon();
    const current = this.value();
    const patch: Partial<TickerRange> = { symbol: t.symbol };
    // Only move the window when the one on screen could not be run against
    // this symbol at all. Rewriting it unconditionally would silently discard
    // a window the operator chose — switching SPY to GLD to compare the same
    // months would jump to the last 30 days instead of comparing anything.
    // This branch never executed before the lake-backed catalog: no entry in
    // the constant it replaced carried a date, so `pickTicker` only ever
    // changed the symbol.
    if (t.lastHeld && !overlapsHeldRange(current, t)) {
      // Sidecar validator rejects weekend endpoints with 422. ``last``
      // arrives from a data-availability response so it's usually
      // already a weekday, but ``last - 30 days`` lands on a weekend
      // whenever ``last`` falls Mon-Wed. Operate entirely on the ISO
      // string via ``toMostRecentTradingDayIso`` so the walk is
      // UTC-internal — the earlier ``Date``-based path silently
      // re-introduced a weekend in west-of-UTC browsers because
      // ``new Date("YYYY-MM-DD")`` parses as UTC midnight (= prior
      // local evening), the local-time walk then stepped onto a
      // local Friday whose UTC instant fell on Saturday, and
      // ``isoDate``'s UTC ``toISOString`` round-trip emitted the
      // Saturday day stamp (PR #346 P1 review).
      const start = toMostRecentTradingDayIso(t.lastHeld, -30);
      // Clamp to where the held range begins. A thinly-backfilled symbol
      // would otherwise open on a 30-day window of which almost none is
      // readable, and the run would refuse data the picker had proposed.
      // `firstHeld` is a real trading date, so it needs no weekday walk —
      // walking it would step before the range and defeat the clamp.
      patch.from = t.firstHeld && t.firstHeld > start ? t.firstHeld : start;
      patch.to = toMostRecentTradingDayIso(t.lastHeld);
    }
    this.value.set({ ...current, ...patch });
    this.closeDropdown();
  }
}

import {
  ChangeDetectionStrategy,
  Component,
  computed,
  effect,
  ElementRef,
  inject,
  input,
  model,
  signal,
  untracked,
  viewChild,
} from '@angular/core';
import { RouterLink } from '@angular/router';
import { Tooltip } from 'primeng/tooltip';

import {
  type TickerOption,
  type TickerRange,
} from '../ticker-range-picker.types';
import { DEFAULT_ADJUSTMENT_MODE, TickerCatalogService } from '../../ticker-catalog';
import type { PriceAdjustmentMode } from '../../data-lake';
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
  imports: [RouterLink, Tooltip],
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './instrument-card.component.html',
  styleUrls: ['./instrument-card.component.scss'],
  host: {
    '(document:mousedown)': 'onDocumentMouseDown($event)',
  },
})
export class InstrumentCardComponent {
  readonly value = model.required<TickerRange>();
  readonly appearance = input<'card' | 'flat'>('card');

  /**
   * The tree this picker's run will read. Belongs to the host page — Strategy
   * Lab sends `adjusted: false` for a `both` run, Data Lab has its own
   * toggle — because coverage differs between trees.
   */
  readonly adjustmentMode = input<PriceAdjustmentMode>(DEFAULT_ADJUSTMENT_MODE);

  /**
   * A universe supplied by the host, for pickers whose subject is not lake
   * bars. Ticker Explorer's snapshot hits Polygon live and can query symbols
   * the lake has never held, so imposing the backtest universe there removes
   * working functionality. `null` (the default) means "ask the lake".
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

  // Lake-backed membership is a system-wide fact per mode, not a property of
  // whichever page mounted the picker; it used to be drilled in as an input
  // from nine hosts, every one passing the same hardcoded constant.
  readonly tickerPool = computed<readonly TickerOption[]>(
    () => this.universe() ?? this.view().pool(),
  );
  readonly recent = computed<readonly string[]>(() =>
    this.universe() === null ? this.view().recent() : [],
  );
  readonly catalogLoading = computed(() =>
    this.universe() === null ? this.view().loading() : false,
  );
  readonly catalogUnavailable = computed<string | null>(() =>
    this.universe() === null ? this.view().unavailable() : null,
  );
  /** A host-supplied universe is not the lake, so lake copy must not appear. */
  readonly lakeBacked = computed(() => this.universe() === null);

  private readonly rootEl =
    viewChild.required<ElementRef<HTMLElement>>('rootEl');
  private readonly searchInput =
    viewChild<ElementRef<HTMLInputElement>>('searchInput');

  readonly open = signal(false);
  readonly query = signal('');

  constructor() {
    effect(() => {
      if (this.open()) {
        const input = this.searchInput();
        if (input) queueMicrotask(() => input.nativeElement.focus());
      }
    });
  }

  readonly selectedTicker = computed<TickerOption | undefined>(() =>
    this.tickerPool().find((t) => t.symbol === this.value().symbol),
  );

  readonly selectedFirstHeld = computed<string | null>(
    () => this.selectedTicker()?.firstHeld ?? null,
  );

  readonly selectedLastHeld = computed<string | null>(
    () => this.selectedTicker()?.lastHeld ?? null,
  );

  /** The lake answered, and holds nothing at all — distinct from no match. */
  readonly catalogEmpty = computed(
    () =>
      this.tickerPool().length === 0 &&
      !this.catalogLoading() &&
      this.catalogUnavailable() === null,
  );

  readonly selectedExchange = computed(
    () => this.selectedTicker()?.exchange ?? '—',
  );

  readonly selectedExchangeTooltip = computed<string>(() => {
    const code = this.selectedExchange();
    const symbol = this.value().symbol;
    const name = EXCHANGE_NAMES[code];
    if (!name) {
      return 'Listing exchange — where this instrument is primarily traded.';
    }
    return `${name} — primary listing venue for ${symbol}.`;
  });

  readonly filteredTickers = computed<readonly TickerOption[]>(() => {
    const q = this.query().trim().toUpperCase();
    const pool = this.tickerPool();
    if (!q) return pool;
    return pool.filter(
      (t) => t.symbol.includes(q) || t.name.toUpperCase().includes(q),
    );
  });

  readonly recentTickers = computed<readonly TickerOption[]>(() => {
    const recent = this.recent();
    if (recent.length === 0) return [];
    const pool = this.tickerPool();
    return recent
      .map((s) => pool.find((t) => t.symbol === s))
      .filter((t): t is TickerOption => !!t);
  });

  retryCatalog(): void {
    this.view().reload();
  }

  trackBySymbol(_: number, t: TickerOption): string {
    return t.symbol;
  }

  openDropdown(): void {
    if (this.open()) return;
    this.open.set(true);
    this.query.set('');
    // Re-read on open so a symbol backfilled since this tab loaded is
    // selectable without a page reload. `resource.reload()` is a no-op while
    // one is already in flight, so opening repeatedly costs at most one read.
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

  pickTicker(t: TickerOption): void {
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

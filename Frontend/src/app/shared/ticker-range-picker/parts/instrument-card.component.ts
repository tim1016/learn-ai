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
  viewChild,
} from '@angular/core';
import { RouterLink } from '@angular/router';
import { Tooltip } from 'primeng/tooltip';

import {
  type TickerOption,
  type TickerRange,
} from '../ticker-range-picker.types';
import { TickerCatalogService } from '../../ticker-catalog';
import { toMostRecentTradingDayIso } from '../../date/weekday';

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

  // The pool is a system-wide fact — what the lake holds bars for — not a
  // property of whichever page mounted the picker. It used to be drilled in
  // as an input from nine hosts, every one of them passing the same constant.
  private readonly catalog = inject(TickerCatalogService);
  readonly tickerPool = this.catalog.pool;
  readonly recent = this.catalog.recent;
  readonly catalogLoading = this.catalog.loading;
  readonly catalogUnavailable = this.catalog.unavailable;

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

  readonly selectedTickerFirst = computed<string | null>(
    () => this.selectedTicker()?.first ?? null,
  );

  readonly selectedTickerLast = computed<string | null>(
    () => this.selectedTicker()?.last ?? null,
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
    this.catalog.reload();
  }

  trackBySymbol(_: number, t: TickerOption): string {
    return t.symbol;
  }

  openDropdown(): void {
    if (this.open()) return;
    this.open.set(true);
    this.query.set('');
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
    if (t.last) {
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
      const start = toMostRecentTradingDayIso(t.last, -30);
      // Clamp to where coverage actually begins. A thinly-backfilled symbol
      // (a few days in the lake) would otherwise open on a 30-day window of
      // which almost none is readable, and the run would refuse on data the
      // picker itself had proposed.
      patch.from = t.first && t.first > start ? t.first : start;
      patch.to = toMostRecentTradingDayIso(t.last);
    }
    this.value.set({ ...current, ...patch });
    this.closeDropdown();
  }
}

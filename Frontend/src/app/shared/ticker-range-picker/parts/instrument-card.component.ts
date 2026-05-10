import {
  ChangeDetectionStrategy,
  Component,
  computed,
  effect,
  ElementRef,
  HostListener,
  input,
  model,
  signal,
  viewChild,
} from '@angular/core';
import { CommonModule } from '@angular/common';
import { FormsModule } from '@angular/forms';
import { Tooltip } from 'primeng/tooltip';

import {
  isoDate,
  type TickerOption,
  type TickerRange,
} from '../ticker-range-picker.types';

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
  imports: [CommonModule, FormsModule, Tooltip],
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './instrument-card.component.html',
  styleUrls: ['./instrument-card.component.scss'],
})
export class InstrumentCardComponent {
  readonly value = model.required<TickerRange>();
  readonly tickerPool = input<readonly TickerOption[]>([]);
  readonly recent = input<readonly string[]>([]);

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

  readonly selectedTickerCachePct = computed<number | null>(() => {
    const cache = this.selectedTicker()?.cache;
    return typeof cache === 'number' ? cache : null;
  });

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

  @HostListener('document:mousedown', ['$event'])
  onDocumentMouseDown(event: MouseEvent): void {
    const host = this.rootEl().nativeElement;
    if (!host.contains(event.target as Node)) {
      this.closeDropdown();
    }
  }

  onSearchInput(value: string): void {
    this.query.set(value);
  }

  pickTicker(t: TickerOption): void {
    const current = this.value();
    const patch: Partial<TickerRange> = { symbol: t.symbol };
    if (t.last) {
      const end = new Date(t.last);
      const start = new Date(end);
      start.setDate(start.getDate() - 30);
      patch.from = isoDate(start);
      patch.to = isoDate(end);
    }
    this.value.set({ ...current, ...patch });
    this.closeDropdown();
  }

  cacheTextColor(pct: number | undefined): string {
    if (pct === undefined) return 'var(--text-muted)';
    if (pct >= 0.9) return 'var(--bull)';
    if (pct >= 0.5) return 'var(--warn)';
    return 'var(--text-muted)';
  }

  cacheLabel(pct: number | undefined): string {
    if (pct === undefined || pct === 0) return 'no cache';
    return `${Math.round(pct * 100)}%`;
  }
}

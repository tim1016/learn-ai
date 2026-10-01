import { ChangeDetectionStrategy, Component, computed, inject, input, signal } from '@angular/core';
import { RouterLink } from '@angular/router';
import { ButtonModule } from 'primeng/button';

import { AssetIdentityComponent } from '../../shared/asset-identity/asset-identity.component';
import { ReceiptLabelPipe } from '../../shared/pipes/receipt-label.pipe';
import { SymbolPickerComponent } from '../../shared/symbol-picker/symbol-picker.component';
import type { TickerOption } from '../../shared/ticker-range-picker/ticker-range-picker.types';
import { TimestampDisplayComponent } from '../../shared/timestamp';
import { GoldenSearchService } from './golden-search.service';
import type { StrategyCapability, StudyListFilters, StudySummary } from './golden-search.types';

/**
 * Golden Search history (#2696): every study, newest first, filterable by
 * strategy and instrument, hidden studies on request. Each row says enough to
 * judge it without opening it: method, state, engine runs used, the final
 * test's claim and outcome. A study is opened on its own page.
 */
@Component({
  selector: 'app-golden-search-history',
  imports: [AssetIdentityComponent, ButtonModule, ReceiptLabelPipe, RouterLink, SymbolPickerComponent, TimestampDisplayComponent],
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './golden-search-history.component.html',
  styleUrl: './golden-search-history.component.scss',
})
export class GoldenSearchHistoryComponent {
  private readonly service = inject(GoldenSearchService);

  readonly capabilities = input<readonly StrategyCapability[]>([]);

  readonly rows = signal<StudySummary[]>([]);
  readonly loading = signal(false);
  readonly error = signal<string | null>(null);
  readonly filters = signal<StudyListFilters>({});
  /** Every instrument any listing has shown: the symbol filter's closed universe (a filter must not trigger a backfill). */
  private readonly seenSymbols = signal<readonly string[]>([]);

  protected readonly displayNames = computed(() => new Map(this.capabilities().map((c) => [c.strategy_key, c.display_name])));
  protected readonly symbolUniverse = computed<readonly TickerOption[]>(() => this.seenSymbols().map((symbol) => ({ symbol, name: symbol })));

  constructor() {
    void this.refresh();
  }

  /** Generation of the latest request; an older response must not overwrite a newer filter's rows. */
  private generation = 0;

  async refresh(): Promise<void> {
    const generation = ++this.generation;
    this.loading.set(true);
    try {
      const rows = await this.service.list(this.filters());
      if (generation !== this.generation) return;
      this.rows.set(rows);
      this.seenSymbols.update((seen) => [...new Set([...seen, ...rows.map((row) => row.symbol)])].sort());
      this.error.set(null);
    } catch {
      if (generation === this.generation) this.error.set('History could not be loaded.');
    } finally {
      if (generation === this.generation) this.loading.set(false);
    }
  }

  onStrategy(event: Event): void {
    if (event.target instanceof HTMLSelectElement) this.setFilter({ strategy_key: event.target.value || undefined });
  }

  onSymbol(symbol: string): void {
    this.setFilter({ symbol: symbol || undefined });
  }

  onIncludeHidden(event: Event): void {
    if (event.target instanceof HTMLInputElement) this.setFilter({ include_hidden: event.target.checked || undefined });
  }

  displayName(strategyKey: string): string | null {
    return this.displayNames().get(strategyKey) ?? null;
  }

  private setFilter(patch: StudyListFilters): void {
    this.filters.update((filters) => ({ ...filters, ...patch }));
    void this.refresh();
  }
}

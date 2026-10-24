import { ChangeDetectionStrategy, Component, computed, effect, inject, input, signal, untracked } from '@angular/core';
import { RouterLink } from '@angular/router';
import { ButtonModule } from 'primeng/button';

import { AssetIdentityComponent } from '../../shared/asset-identity/asset-identity.component';
import { extractServerMessage } from '../broker/operation-error';
import { ReceiptLabelPipe } from '../../shared/pipes/receipt-label.pipe';
import { SymbolPickerComponent } from '../../shared/symbol-picker/symbol-picker.component';
import type { TickerOption } from '../../shared/ticker-range-picker/ticker-range-picker.types';
import { TimestampDisplayComponent } from '../../shared/timestamp';
import { goldenStudyRoute } from '../../signal-strategies/golden/golden-tabs';
import { GoldenSearchService } from './golden-search.service';
import type { StudyListFilters, StudySummary } from './golden-search.types';

/** How many of the newest studies one read asks for; a full page may be cut short of older ones. */
const HISTORY_LIMIT = 100;

/**
 * Golden Search history (#2696): one strategy's studies, newest first, filterable by
 * instrument, hidden studies on
 * request. Each row says enough to judge it without opening it: method, state,
 * engine runs used, the final test's claim and outcome. A study is opened on its
 * own page, under the strategy it ran for.
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

  /** The one strategy this history is for: its studies only. */
  readonly strategyKey = input.required<string>();

  readonly rows = signal<StudySummary[]>([]);
  readonly loading = signal(false);
  readonly error = signal<string | null>(null);
  readonly filters = signal<StudyListFilters>({});
  /** Every instrument any listing has shown: the symbol filter's closed universe (a filter must not trigger a backfill). */
  private readonly seenSymbols = signal<readonly string[]>([]);

  protected readonly limit = HISTORY_LIMIT;
  protected readonly capped = computed(() => this.rows().length >= HISTORY_LIMIT);
  protected readonly symbolUniverse = computed<readonly TickerOption[]>(() => this.seenSymbols().map((symbol) => ({ symbol, name: symbol })));

  constructor() {
    // Read once the host's inputs are in, so a pinned strategy filters the very first read and again if it changes.
    effect(() => {
      this.strategyKey();
      untracked(() => void this.refresh());
    });
  }

  /** Generation of the latest request; an older response must not overwrite a newer filter's rows. */
  private generation = 0;

  async refresh(): Promise<void> {
    const generation = ++this.generation;
    this.loading.set(true);
    try {
      const rows = await this.service.list({ ...this.filters(), strategy_key: this.strategyKey(), limit: HISTORY_LIMIT });
      if (generation !== this.generation) return;
      this.rows.set(rows);
      this.seenSymbols.update((seen) => [...new Set([...seen, ...rows.map((row) => row.symbol)])].sort());
      this.error.set(null);
    } catch (error) {
      if (generation === this.generation) this.error.set(extractServerMessage(error, 'History could not be loaded.'));
    } finally {
      if (generation === this.generation) this.loading.set(false);
    }
  }

  onSymbol(symbol: string): void {
    this.setFilter({ symbol: symbol || undefined });
  }

  onIncludeHidden(event: Event): void {
    if (event.target instanceof HTMLInputElement) this.setFilter({ include_hidden: event.target.checked || undefined });
  }

  /** The study's own page, under the strategy that ran it. */
  protected studyRoute(row: StudySummary): readonly string[] {
    return goldenStudyRoute(row.strategy_key, row.id);
  }

  private setFilter(patch: StudyListFilters): void {
    this.filters.update((filters) => ({ ...filters, ...patch }));
    void this.refresh();
  }
}

import { ChangeDetectionStrategy, Component, computed, inject, input, output, signal } from '@angular/core';
import { ButtonModule } from 'primeng/button';
import { InputText } from 'primeng/inputtext';

import { AssetIdentityComponent } from '../../shared/asset-identity/asset-identity.component';
import { ReceiptLabelPipe } from '../../shared/pipes/receipt-label.pipe';
import { TimestampDisplayComponent } from '../../shared/timestamp';
import type { StrategyInfo } from '../strategy-lab/strategy-lab.models';
import { GridSearchService } from './grid-search.service';
import type { GridSearchHistoryFilters, GridSearchStatus, GridSearchSummary } from './grid-search.types';

const STATUSES: readonly GridSearchStatus[] = ['queued', 'running', 'completed', 'failed', 'cancelled', 'interrupted'];

/**
 * Grid Search history (PRD #1926): every user-launched search, newest first,
 * filterable by strategy, symbol and status, each row judged without opening
 * it, with per-row delete behind a confirmation.
 */
@Component({
  selector: 'app-grid-search-history',
  imports: [AssetIdentityComponent, ButtonModule, InputText, ReceiptLabelPipe, TimestampDisplayComponent],
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './grid-search-history.component.html',
  styleUrl: './grid-search-history.component.scss',
})
export class GridSearchHistoryComponent {
  private readonly service = inject(GridSearchService);

  readonly strategies = input<readonly StrategyInfo[]>([]);
  readonly opened = output<string>();

  readonly rows = signal<GridSearchSummary[]>([]);
  readonly loading = signal(false);
  readonly error = signal<string | null>(null);
  readonly filters = signal<GridSearchHistoryFilters>({});
  readonly confirming = signal<string | null>(null);

  protected readonly statuses = STATUSES;
  protected readonly displayNames = computed(() => new Map(this.strategies().map((s) => [s.name, s.display_name])));
  /** Only sweepable strategies can have a search, so only they are offered as a filter. */
  protected readonly filterableStrategies = computed(() => this.strategies().filter((s) => s.sweep_eligibility?.eligible === true));

  constructor() {
    void this.refresh();
  }

  async refresh(): Promise<void> {
    this.loading.set(true);
    try {
      this.rows.set(await this.service.list(this.filters()));
      this.error.set(null);
    } catch {
      this.error.set('History could not be loaded.');
    } finally {
      this.loading.set(false);
    }
  }

  onFilterEvent(key: keyof GridSearchHistoryFilters, event: Event): void {
    const target = event.target;
    if (target instanceof HTMLSelectElement || target instanceof HTMLInputElement) this.setFilter(key, target.value);
  }

  setFilter(key: keyof GridSearchHistoryFilters, raw: string): void {
    this.filters.update((current) => {
      const kept = Object.fromEntries(Object.entries(current).filter(([name]) => name !== key)) as GridSearchHistoryFilters;
      return raw ? { ...kept, [key]: raw } : kept;
    });
    void this.refresh();
  }

  displayName(strategyKey: string): string | null {
    return this.displayNames().get(strategyKey) ?? null;
  }

  leaderText(row: GridSearchSummary): string {
    if (!row.leader_params) return row.status === 'completed' ? 'No eligible leader' : '—';
    const text = Object.entries(row.leader_params).map(([k, v]) => `${k}=${v}`).join(', ');
    return row.incomplete ? `${text} (provisional)` : text;
  }

  requestDelete(id: string): void {
    this.confirming.set(id);
  }

  cancelDelete(): void {
    this.confirming.set(null);
  }

  async confirmDelete(id: string): Promise<void> {
    try {
      await this.service.delete(id);
      this.rows.update((rows) => rows.filter((row) => row.id !== id));
    } catch {
      this.error.set('The search could not be deleted. If it is running, try again once cancellation is acknowledged.');
    } finally {
      this.confirming.set(null);
    }
  }
}

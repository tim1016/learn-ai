import { ChangeDetectionStrategy, Component, computed, inject, input, output, signal } from '@angular/core';
import { ButtonModule } from 'primeng/button';

import { AssetIdentityComponent } from '../../shared/asset-identity/asset-identity.component';
import { ReceiptLabelPipe } from '../../shared/pipes/receipt-label.pipe';
import { ConfirmDeleteComponent } from '../../shared/research-record/confirm-delete.component';
import { ResearchHistoryFiltersComponent, type ResearchHistoryFilters } from '../../shared/research-record/research-history-filters.component';
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
  imports: [AssetIdentityComponent, ButtonModule, ConfirmDeleteComponent, ReceiptLabelPipe, ResearchHistoryFiltersComponent, TimestampDisplayComponent],
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

  protected readonly statuses = STATUSES;
  protected readonly displayNames = computed(() => new Map(this.strategies().map((s) => [s.name, s.display_name])));

  constructor() {
    void this.refresh();
  }

  /** Revision of the latest request; an older response must not overwrite a newer filter's rows. */
  private revision = 0;

  async refresh(): Promise<void> {
    const revision = ++this.revision;
    this.loading.set(true);
    try {
      const rows = await this.service.list(this.filters());
      if (revision !== this.revision) return;
      this.rows.set(rows);
      this.error.set(null);
    } catch {
      if (revision === this.revision) this.error.set('History could not be loaded.');
    } finally {
      if (revision === this.revision) this.loading.set(false);
    }
  }

  onFilters(filters: ResearchHistoryFilters): void {
    this.filters.set(filters as GridSearchHistoryFilters);
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

  async delete(id: string): Promise<void> {
    try {
      await this.service.delete(id);
      this.rows.update((rows) => rows.filter((row) => row.id !== id));
    } catch {
      this.error.set('The search could not be deleted. If it is running, try again once cancellation is acknowledged.');
    }
  }
}

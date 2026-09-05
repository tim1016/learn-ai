import { ChangeDetectionStrategy, Component, computed, DestroyRef, effect, inject, input, output, signal, untracked } from '@angular/core';
import { DecimalPipe, KeyValuePipe, PercentPipe } from '@angular/common';
import { RouterLink } from '@angular/router';
import { ButtonModule } from 'primeng/button';

import { ReceiptLabelPipe } from '../../shared/pipes/receipt-label.pipe';
import { TimestampDisplayComponent } from '../../shared/timestamp';
import type { StrategyInfo } from '../strategy-lab/strategy-lab.models';
import { GridSearchService } from './grid-search.service';
import {
  isTerminal,
  type CellPageQuery,
  type CellSortColumn,
  type GridSearchCellPage,
  type GridSearchDetail,
} from './grid-search.types';

const SORTABLE: readonly CellSortColumn[] = ['sharpe_ratio', 'total_return_pct', 'net_profit', 'total_trades', 'max_drawdown_pct', 'win_rate'];
/** Column headings; the engine reports return, drawdown and win rate as fractions, rendered as percentages. */
const COLUMN_LABELS: Readonly<Record<CellSortColumn, string>> = {
  sharpe_ratio: 'Sharpe',
  total_return_pct: 'Total return',
  net_profit: 'Net profit',
  total_trades: 'Trades',
  max_drawdown_pct: 'Max drawdown',
  win_rate: 'Win rate',
  params_hash: 'Settings',
};

/**
 * One search's result (PRD #1926 "Frontend"): status, a non-dismissible
 * in-sample statement, the leader, Finish and Delete, a route into
 * Walk-Forward, and the server-sorted, server-paged cell table. Polls while
 * the search is still running.
 */
@Component({
  selector: 'app-grid-search-result',
  imports: [ButtonModule, DecimalPipe, KeyValuePipe, PercentPipe, RouterLink, ReceiptLabelPipe, TimestampDisplayComponent],
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './grid-search-result.component.html',
  styleUrl: './grid-search-result.component.scss',
})
export class GridSearchResultComponent {
  private readonly service = inject(GridSearchService);
  private readonly destroyRef = inject(DestroyRef);

  readonly searchId = input.required<string>();
  readonly strategies = input<readonly StrategyInfo[]>([]);
  /** Poll interval while the search runs; tests set 0 to disable. */
  readonly pollMs = input(2000);
  readonly deleted = output<string>();
  readonly closed = output();

  readonly detail = signal<GridSearchDetail | null>(null);
  readonly page = signal<GridSearchCellPage | null>(null);
  readonly query = signal<CellPageQuery>({ sort_by: 'sharpe_ratio', direction: 'desc', page: 1, page_size: 25 });
  readonly error = signal<string | null>(null);
  readonly confirmingDelete = signal(false);
  readonly busy = signal(false);

  protected readonly sortable = SORTABLE;
  protected readonly columnLabels = COLUMN_LABELS;
  protected readonly strategyName = computed(() => {
    const key = this.detail()?.strategy_key;
    return key ? (this.strategies().find((s) => s.name === key)?.display_name ?? null) : null;
  });
  protected readonly running = computed(() => {
    const status = this.detail()?.status;
    return status !== undefined && !isTerminal(status);
  });
  protected readonly pageCount = computed(() => {
    const page = this.page();
    return page ? Math.max(1, Math.ceil(page.total / page.page_size)) : 1;
  });
  protected readonly leaderSummary = computed(() => {
    const params = this.detail()?.leader_params;
    return params ? Object.entries(params).map(([key, value]) => `${key}=${value}`).join(', ') : null;
  });

  private timer: ReturnType<typeof setTimeout> | null = null;

  constructor() {
    effect(() => {
      const id = this.searchId();
      untracked(() => void this.reload(id));
    });
    this.destroyRef.onDestroy(() => this.stopPolling());
  }

  async reload(id: string = this.searchId()): Promise<void> {
    try {
      const [detail, page] = await Promise.all([this.service.get(id), this.service.cells(id, this.query())]);
      if (id !== this.searchId()) return;
      this.detail.set(detail);
      this.page.set(page);
      this.error.set(null);
      this.schedulePoll();
    } catch {
      this.error.set('This search could not be loaded.');
    }
  }

  sortBy(column: CellSortColumn): void {
    const current = this.query();
    const direction = current.sort_by === column && current.direction === 'desc' ? 'asc' : 'desc';
    this.query.set({ ...current, sort_by: column, direction, page: 1 });
    void this.reload();
  }

  goToPage(page: number): void {
    this.query.update((q) => ({ ...q, page: Math.min(Math.max(1, page), this.pageCount()) }));
    void this.reload();
  }

  async finish(): Promise<void> {
    const detail = this.detail();
    if (!detail?.resumable) return;
    this.busy.set(true);
    try {
      await this.service.finish(detail);
      await this.reload();
    } catch {
      this.error.set('Finish was not accepted. Reload and check the refusal reason.');
    } finally {
      this.busy.set(false);
    }
  }

  requestDelete(): void {
    this.confirmingDelete.set(true);
  }

  cancelDelete(): void {
    this.confirmingDelete.set(false);
  }

  async confirmDelete(): Promise<void> {
    const id = this.searchId();
    this.busy.set(true);
    try {
      await this.service.delete(id);
      this.stopPolling();
      this.deleted.emit(id);
    } catch {
      this.error.set('The search could not be deleted. If it is running, cancellation is being acknowledged; try again shortly.');
    } finally {
      this.busy.set(false);
      this.confirmingDelete.set(false);
    }
  }

  private schedulePoll(): void {
    this.stopPolling();
    if (!this.running() || this.pollMs() <= 0) return;
    this.timer = setTimeout(() => void this.reload(), this.pollMs());
  }

  private stopPolling(): void {
    if (this.timer !== null) clearTimeout(this.timer);
    this.timer = null;
  }
}

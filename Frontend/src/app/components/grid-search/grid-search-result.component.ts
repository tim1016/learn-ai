import { ChangeDetectionStrategy, Component, computed, DestroyRef, effect, inject, input, output, signal, untracked } from '@angular/core';
import { DecimalPipe, KeyValuePipe, PercentPipe } from '@angular/common';
import { RouterLink } from '@angular/router';
import { ButtonModule } from 'primeng/button';

import { JobsService } from '../../services/jobs.service';
import { AssetIdentityComponent } from '../../shared/asset-identity/asset-identity.component';
import { ReceiptLabelPipe } from '../../shared/pipes/receipt-label.pipe';
import { RecordControlsComponent } from '../../shared/research-record/record-controls.component';
import { RecordPoller } from '../../shared/research-record/record-poller';
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
  imports: [AssetIdentityComponent, ButtonModule, DecimalPipe, KeyValuePipe, PercentPipe, RecordControlsComponent, RouterLink, ReceiptLabelPipe, TimestampDisplayComponent],
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './grid-search-result.component.html',
  styleUrl: './grid-search-result.component.scss',
})
export class GridSearchResultComponent {
  private readonly service = inject(GridSearchService);
  private readonly jobs = inject(JobsService);
  private readonly destroyRef = inject(DestroyRef);

  readonly searchId = input.required<string>();
  readonly strategies = input<readonly StrategyInfo[]>([]);
  /** Poll interval while the search runs; tests set 0 to disable. */
  readonly pollMs = input(2000);
  /** Where the back button returns to: Grid Search history, or the walk-forward study that owns this sweep. */
  readonly backLabel = input('History');
  readonly deleted = output<string>();
  readonly closed = output();

  readonly detail = signal<GridSearchDetail | null>(null);
  readonly page = signal<GridSearchCellPage | null>(null);
  readonly query = signal<CellPageQuery>({ sort_by: 'sharpe_ratio', direction: 'desc', page: 1, page_size: 25 });
  readonly error = signal<string | null>(null);
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
  /** Cells the search has no row for yet — what Finish would run. */
  protected readonly missingCells = computed(() => {
    const d = this.detail();
    return d ? Math.max(0, d.expected_cells - d.completed_cells - d.failed_cells) : 0;
  });
  protected readonly leaderSummary = computed(() => {
    const params = this.detail()?.leader_params;
    return params ? Object.entries(params).map(([key, value]) => `${key}=${value}`).join(', ') : null;
  });

  private readonly poller = new RecordPoller(this.destroyRef);
  /** Set by Finish: keep polling until the row shows the new attempt running (the worker's claim races the 202). */
  private awaitingAttempt = false;

  constructor() {
    effect(() => {
      const id = this.searchId();
      untracked(() => {
        this.page.set(null);
        void this.reload(id);
      });
    });
  }

  /** Revision of the latest reload; a sort, page or poll that resolves late must not restore stale state. */
  private revision = 0;

  async reload(id: string = this.searchId()): Promise<void> {
    const revision = ++this.revision;
    try {
      const detail = await this.service.get(id);
      if (revision !== this.revision) return;
      // The first page of a search sorts by its own ranking measure, so the leader is on it.
      if (this.page() === null) this.query.update((q) => ({ ...q, sort_by: detail.measure }));
      const page = await this.service.cells(id, this.query());
      if (revision !== this.revision) return;
      this.detail.set(detail);
      this.page.set(page);
      this.error.set(null);
      if (!isTerminal(detail.status)) this.awaitingAttempt = false;
      this.schedulePoll();
    } catch {
      if (revision === this.revision) this.error.set('This search could not be loaded.');
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
      this.awaitingAttempt = true;
      await this.reload();
    } catch {
      this.error.set('Finish was not accepted. Reload and check the refusal reason.');
    } finally {
      this.busy.set(false);
    }
  }

  async cancel(): Promise<void> {
    const jobId = this.detail()?.job_id;
    if (!jobId || !this.running()) return;
    this.busy.set(true);
    try {
      await this.jobs.cancelJob(jobId);
      await this.reload();
    } catch {
      this.error.set('Cancellation could not be requested. Try again.');
    } finally {
      this.busy.set(false);
    }
  }

  async delete(): Promise<void> {
    const id = this.searchId();
    this.busy.set(true);
    try {
      await this.service.delete(id);
      this.poller.stop();
      this.deleted.emit(id);
    } catch {
      this.error.set('The search could not be deleted. If it is running, cancellation is being acknowledged; try again shortly.');
    } finally {
      this.busy.set(false);
    }
  }

  private schedulePoll(): void {
    if (this.running() || this.awaitingAttempt) this.poller.schedule(this.pollMs(), () => void this.reload());
    else this.poller.stop();
  }
}

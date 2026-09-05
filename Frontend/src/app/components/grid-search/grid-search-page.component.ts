import { ChangeDetectionStrategy, Component, inject, signal, viewChild } from '@angular/core';
import { Tab, TabList, TabPanel, TabPanels, Tabs } from 'primeng/tabs';

import { PageHeaderComponent } from '../../shared/page-header/page-header.component';
import type { StrategyInfo } from '../strategy-lab/strategy-lab.models';
import { GridSearchFormComponent, type GridSearchLaunch } from './grid-search-form.component';
import { GridSearchHistoryComponent } from './grid-search-history.component';
import { GridSearchResultComponent } from './grid-search-result.component';
import { GridSearchService } from './grid-search.service';

export type GridSearchTab = 'new' | 'history';

/**
 * Grid Search page (PRD #1926): one page, two tabs — configure and launch,
 * and history. A launched search opens in the History tab as soon as its
 * durable record is listable, which the jobs boundary guarantees before it
 * returns the job id.
 */
@Component({
  selector: 'app-grid-search-page',
  imports: [Tabs, TabList, Tab, TabPanels, TabPanel, PageHeaderComponent, GridSearchFormComponent, GridSearchHistoryComponent, GridSearchResultComponent],
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './grid-search-page.component.html',
  styleUrl: './grid-search-page.component.scss',
})
export class GridSearchPageComponent {
  private readonly service = inject(GridSearchService);

  readonly activeTab = signal<GridSearchTab>('new');
  readonly strategies = signal<StrategyInfo[]>([]);
  readonly strategiesError = signal<string | null>(null);
  readonly openSearchId = signal<string | null>(null);
  readonly lookupMessage = signal<string | null>(null);

  private readonly history = viewChild(GridSearchHistoryComponent);

  constructor() {
    void this.loadStrategies();
  }

  async onLaunched(launch: GridSearchLaunch): Promise<void> {
    // The jobs boundary returns the job id only after Python made the record
    // durable, so one lookup by job id is enough.
    this.activeTab.set('history');
    const rows = await this.service.list({ job_id: launch.jobId });
    if (rows.length > 0) {
      this.lookupMessage.set(null);
      this.openSearchId.set(rows[0].id);
      return;
    }
    this.lookupMessage.set('The search was launched but its record was not found in history. Refresh in a moment.');
    void this.history()?.refresh();
  }

  openSearch(id: string): void {
    this.openSearchId.set(id);
  }

  closeSearch(): void {
    this.openSearchId.set(null);
    void this.history()?.refresh();
  }

  onDeleted(): void {
    this.closeSearch();
  }

  setTab(tab: string | number | undefined): void {
    if (tab === 'new' || tab === 'history') this.activeTab.set(tab);
  }

  private async loadStrategies(): Promise<void> {
    try {
      this.strategies.set(await this.service.loadStrategies());
    } catch {
      this.strategiesError.set('The strategy catalogue could not be loaded.');
    }
  }
}

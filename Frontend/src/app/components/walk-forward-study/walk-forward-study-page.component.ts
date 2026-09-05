import { ChangeDetectionStrategy, Component, inject, signal, viewChild } from '@angular/core';
import { takeUntilDestroyed } from '@angular/core/rxjs-interop';
import { ActivatedRoute } from '@angular/router';
import { Tab, TabList, TabPanel, TabPanels, Tabs } from 'primeng/tabs';

import { PageHeaderComponent } from '../../shared/page-header/page-header.component';
import { GridSearchService } from '../grid-search/grid-search.service';
import type { GridSearchSpecRequest } from '../grid-search/grid-search.types';
import type { StrategyInfo } from '../strategy-lab/strategy-lab.models';
import { WalkForwardStudyFormComponent, type WalkForwardStudyLaunch } from './walk-forward-study-form.component';
import { WalkForwardStudyHistoryComponent } from './walk-forward-study-history.component';
import { WalkForwardStudyResultComponent } from './walk-forward-study-result.component';
import { WalkForwardStudyService } from './walk-forward-study.service';

export type WalkForwardStudyTab = 'new' | 'history';

/**
 * Walk-Forward page (PRD #1925): one page, two tabs — configure and launch a
 * study, and history. `?search=<grid search id>` seeds the form from a
 * completed grid search (the hand-off from its in-sample result). A launched
 * study opens in History as soon as its record is listable, which the jobs
 * boundary guarantees before it returns the job id.
 */
@Component({
  selector: 'app-walk-forward-study-page',
  imports: [Tabs, TabList, Tab, TabPanels, TabPanel, PageHeaderComponent, WalkForwardStudyFormComponent, WalkForwardStudyHistoryComponent, WalkForwardStudyResultComponent],
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './walk-forward-study-page.component.html',
  styleUrl: './walk-forward-study-page.component.scss',
})
export class WalkForwardStudyPageComponent {
  private readonly service = inject(WalkForwardStudyService);
  private readonly gridSearches = inject(GridSearchService);
  private readonly route = inject(ActivatedRoute);

  readonly activeTab = signal<WalkForwardStudyTab>('new');
  readonly strategies = signal<StrategyInfo[]>([]);
  readonly strategiesError = signal<string | null>(null);
  readonly prefill = signal<GridSearchSpecRequest | null>(null);
  readonly prefillMessage = signal<string | null>(null);
  readonly openStudyId = signal<string | null>(null);
  readonly lookupMessage = signal<string | null>(null);

  private readonly history = viewChild(WalkForwardStudyHistoryComponent);

  constructor() {
    void this.loadStrategies();
    this.route.queryParamMap.pipe(takeUntilDestroyed()).subscribe((params) => {
      const searchId = params.get('search');
      if (searchId) void this.loadPrefill(searchId);
    });
  }

  async onLaunched(launch: WalkForwardStudyLaunch): Promise<void> {
    this.activeTab.set('history');
    const rows = await this.service.list({ job_id: launch.jobId });
    if (rows.length > 0) {
      this.lookupMessage.set(null);
      this.openStudyId.set(rows[0].id);
      return;
    }
    this.lookupMessage.set('The study was launched but its record was not found in history. Refresh in a moment.');
    void this.history()?.refresh();
  }

  openStudy(id: string): void {
    this.openStudyId.set(id);
  }

  closeStudy(): void {
    this.openStudyId.set(null);
    void this.history()?.refresh();
  }

  setTab(tab: string | number | undefined): void {
    if (tab === 'new' || tab === 'history') this.activeTab.set(tab);
  }

  private async loadStrategies(): Promise<void> {
    try {
      this.strategies.set(await this.gridSearches.loadStrategies());
    } catch {
      this.strategiesError.set('The strategy catalogue could not be loaded.');
    }
  }

  private async loadPrefill(searchId: string): Promise<void> {
    try {
      const detail = await this.gridSearches.get(searchId);
      this.prefill.set(detail.request);
      this.prefillMessage.set(`Grid, window and costs copied from grid search ${searchId.slice(0, 8)}. Choose the training and test lengths.`);
    } catch {
      this.prefillMessage.set('The grid search to start from could not be loaded; the form starts empty.');
    }
  }
}

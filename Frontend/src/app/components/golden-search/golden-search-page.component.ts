import { ChangeDetectionStrategy, Component, effect, inject, input, signal, untracked } from '@angular/core';
import { Router } from '@angular/router';
import { Tab, TabList, TabPanel, TabPanels, Tabs } from 'primeng/tabs';

import { PageHeaderComponent } from '../../shared/page-header/page-header.component';
import { GoldenSearchHistoryComponent } from './golden-search-history.component';
import { GoldenSearchPlanFormComponent } from './golden-search-plan-form.component';
import { GoldenSearchStudyComponent } from './golden-search-study.component';
import { GoldenSearchService } from './golden-search.service';
import type { StrategyCapability, StudyDetail } from './golden-search.types';

export type GoldenSearchTab = 'new' | 'history';

/**
 * Golden Search (#2696). `/golden-search` plans a new study and lists the
 * history; `?revise=<id>` starts the plan from that study's frozen protocol
 * (locking it creates a new linked study); `/golden-search/<id>` opens one
 * study's workbench. A locked plan navigates straight to its study.
 */
@Component({
  selector: 'app-golden-search-page',
  imports: [GoldenSearchHistoryComponent, GoldenSearchPlanFormComponent, GoldenSearchStudyComponent, PageHeaderComponent, Tab, TabList, TabPanel, TabPanels, Tabs],
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './golden-search-page.component.html',
  styleUrl: './golden-search-page.component.scss',
})
export class GoldenSearchPageComponent {
  private readonly service = inject(GoldenSearchService);
  private readonly router = inject(Router);

  /** Route parameter: the study to open. */
  readonly studyId = input<string | undefined>(undefined);
  /** Query parameter: the study whose frozen plan a new study revises. */
  readonly revise = input<string | undefined>(undefined);

  readonly activeTab = signal<GoldenSearchTab>('new');
  /** Null until the declarations have loaded; the plan form waits for them rather than claim there are none. */
  readonly capabilities = signal<StrategyCapability[] | null>(null);
  readonly capabilitiesError = signal<string | null>(null);
  readonly reviseFrom = signal<StudyDetail | null>(null);
  readonly reviseError = signal<string | null>(null);

  constructor() {
    void this.loadCapabilities();
    effect(() => {
      const id = this.revise();
      untracked(() => void this.loadRevision(id));
    });
  }

  setTab(tab: string | number | undefined): void {
    if (tab === 'new' || tab === 'history') this.activeTab.set(tab);
  }

  onLocked(studyId: string): void {
    void this.router.navigate(['/golden-search', studyId]);
  }

  onHidden(): void {
    this.activeTab.set('history');
    void this.router.navigate(['/golden-search']);
  }

  private async loadCapabilities(): Promise<void> {
    try {
      this.capabilities.set(await this.service.capabilities());
    } catch {
      this.capabilitiesError.set('The Golden Search strategy declarations could not be loaded.');
    }
  }

  /** Generation of the latest revise request; an older study must not replace a newer one. */
  private reviseGeneration = 0;

  private async loadRevision(id: string | undefined): Promise<void> {
    const generation = ++this.reviseGeneration;
    this.reviseFrom.set(null);
    this.reviseError.set(null);
    if (!id) return;
    this.activeTab.set('new');
    try {
      const study = await this.service.get(id);
      if (generation === this.reviseGeneration) this.reviseFrom.set(study);
    } catch {
      if (generation === this.reviseGeneration) this.reviseError.set('The study to revise could not be loaded; the plan starts from the defaults instead.');
    }
  }
}

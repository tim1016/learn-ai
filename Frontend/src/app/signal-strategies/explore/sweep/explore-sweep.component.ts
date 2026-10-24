import { HttpErrorResponse } from '@angular/common/http';
import { ChangeDetectionStrategy, Component, computed, effect, inject, input, resource, signal, untracked, viewChild } from '@angular/core';
import { Router } from '@angular/router';
import { Tab, TabList, TabPanel, TabPanels, Tabs } from 'primeng/tabs';

import { GridSearchFormComponent, type GridSearchLaunch } from '../../../components/grid-search/grid-search-form.component';
import { GridSearchHistoryComponent } from '../../../components/grid-search/grid-search-history.component';
import { GridSearchResultComponent } from '../../../components/grid-search/grid-search-result.component';
import { GridSearchService } from '../../../components/grid-search/grid-search.service';
import type { WorkspaceStateContent } from '../../../shared/workspace/workspace-state';
import { WorkspaceStateBlockComponent } from '../../../shared/workspace/workspace-state-block.component';
import { readStateOf } from '../../read-state';
import { ExploreSession } from '../explore-session';
import { EXPLORE_RUN_PARAM, EXPLORE_SEARCH_PARAM, exploreTabRoute } from '../explore-tabs';
import { goldenTabRoute } from '../../golden/golden-tabs';
import { injectBacktestHandoff } from '../handoff/backtest-handoff';
import { ExploreHandoffNoticeComponent } from '../handoff/explore-handoff-notice.component';

type SweepTab = 'new' | 'history';

/**
 * Explore's Sweep (PRD #1926): configure and launch a Grid Search for this
 * strategy, and its history. `?search=` opens one search's result, so the search
 * on screen is the address's; `?run=` fills the new-search form from a saved
 * backtest, which only fills the form in: the owner reviews it and launches.
 *
 * A search another strategy ran, or a run another strategy produced, is refused
 * in words and never drawn under this strategy's header. A launched search opens
 * in History as soon as its durable record is listable, which the jobs boundary
 * guarantees before it returns the job id.
 */
@Component({
  selector: 'app-explore-sweep',
  imports: [
    Tabs,
    TabList,
    Tab,
    TabPanels,
    TabPanel,
    ExploreHandoffNoticeComponent,
    GridSearchFormComponent,
    GridSearchHistoryComponent,
    GridSearchResultComponent,
    WorkspaceStateBlockComponent,
  ],
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './explore-sweep.component.html',
  styleUrl: './explore-sweep.component.scss',
})
export class ExploreSweepComponent {
  private readonly service = inject(GridSearchService);
  private readonly session = inject(ExploreSession);
  private readonly router = inject(Router);

  /** The saved Grid Search the address opens (`?search=`). */
  readonly search = input<string | undefined>(undefined);
  /** The saved backtest the new-search form starts from (`?run=`). */
  readonly run = input<string | undefined>(undefined);

  protected readonly key = this.session.key;
  protected readonly walkForwardRoute = computed(() => exploreTabRoute(this.key(), 'walk-forward'));
  /** Golden plans a search from this one's ranges, where the registry allows a golden search at all. */
  protected readonly goldenRoute = computed(() => (this.session.goldenCapability()?.supported === true ? goldenTabRoute(this.key(), 'studies') : null));

  readonly activeTab = signal<SweepTab>('new');
  readonly lookupMessage = signal<string | null>(null);

  private readonly history = viewChild(GridSearchHistoryComponent);

  /** Strategy Lab's entry for this strategy: the only strategy this form offers. */
  protected readonly strategies = computed(() => {
    const catalog = this.session.catalog();
    return catalog.kind === 'ready' && catalog.value !== null ? [catalog.value] : [];
  });

  protected readonly handoff = injectBacktestHandoff('sweep', this.run);
  protected readonly seed = computed(() => {
    const state = this.handoff();
    return state?.kind === 'ready' ? state.handoff.spec : null;
  });

  private readonly searchRead = resource({
    params: () => this.search() || undefined,
    loader: ({ params }) => this.service.get(params),
  });

  /** What the address's `?search=` is: not asked, being read, refused, or this strategy's own search. */
  protected readonly searchState = computed<
    | { readonly kind: 'none' }
    | { readonly kind: 'loading' }
    | { readonly kind: 'refused'; readonly state: WorkspaceStateContent }
    | { readonly kind: 'own'; readonly id: string }
  >(() => {
    const id = this.search();
    if (!id) return { kind: 'none' };
    const read = readStateOf(this.searchRead);
    if (read.kind === 'loading') return { kind: 'loading' };
    if (read.kind === 'failed') {
      const failure: unknown = this.searchRead.error();
      const gone = failure instanceof HttpErrorResponse && failure.status === 404;
      return {
        kind: 'refused',
        state: {
          kind: 'unavailable',
          happened: gone ? `Grid search ${id} was not found.` : `Grid search ${id} could not be read.`,
          why: gone ? 'The server holds no search with that id.' : read.message,
          next: 'Choose a search from this strategy’s history.',
        },
      };
    }
    const owner = read.value.strategy_key;
    if (owner !== this.key()) {
      return {
        kind: 'refused',
        state: {
          kind: 'unavailable',
          happened: `Grid search ${id.slice(0, 8)} belongs to ${owner}, not ${this.key()}.`,
          why: 'A search opens only under the strategy that ran it, so this page shows none of its result.',
          next: 'Open it under its own strategy, or choose a search from this strategy’s history.',
          actions: [
            {
              id: 'owner',
              label: `Open it under ${owner}`,
              route: exploreTabRoute(owner, 'sweep'),
              queryParams: { [EXPLORE_SEARCH_PARAM]: id },
            },
          ],
        },
      };
    }
    return { kind: 'own', id };
  });

  /** Why the tool cannot be used at all for this strategy, or `null`. */
  protected readonly blocked = computed<WorkspaceStateContent | null>(() => {
    const capability = this.session.sweepCapability();
    if (capability !== null && !capability.supported) {
      return {
        kind: 'unavailable',
        happened: 'The registry does not allow a sweep of this strategy.',
        why: capability.reason,
        next: 'Backtest it instead, or choose a strategy the registry allows.',
      };
    }
    const catalog = this.session.catalog();
    if (catalog.kind === 'loading') return { kind: 'checking', happened: "Reading the strategy's declared settings." };
    if (catalog.kind === 'failed') {
      return {
        kind: 'failed',
        happened: "The strategy's declared settings could not be read.",
        why: catalog.message,
        next: 'A sweep needs them to build its grid.',
        actions: [{ id: 'retry', label: 'Read them again' }],
      };
    }
    if (catalog.value === null) {
      return {
        kind: 'unavailable',
        happened: `Strategy Lab's catalog does not offer ${this.key()}.`,
        why: 'Python declares no settings for it, so there is no grid to build.',
        next: 'Backtest it instead, or choose another strategy.',
      };
    }
    return null;
  });

  constructor() {
    this.session.wantCatalog();
    // A search in the address is read in History, where searches live.
    effect(() => {
      if (this.search()) untracked(() => this.activeTab.set('history'));
    });
  }

  onAction(actionId: string): void {
    if (actionId === 'retry') this.session.retryCatalog();
  }

  async onLaunched(launch: GridSearchLaunch): Promise<void> {
    // The jobs boundary returns the job id only after Python made the record
    // durable, so one lookup by job id is enough.
    this.activeTab.set('history');
    const rows = await this.service.list({ job_id: launch.jobId });
    if (rows.length > 0) {
      this.lookupMessage.set(null);
      this.openResult(rows[0].id);
      return;
    }
    this.lookupMessage.set('The search was launched but its record was not found in history. Refresh in a moment.');
    void this.history()?.refresh();
  }

  openResult(id: string): void {
    void this.router.navigate([...exploreTabRoute(this.key(), 'sweep')], {
      queryParams: { [EXPLORE_SEARCH_PARAM]: id, [EXPLORE_RUN_PARAM]: null },
      queryParamsHandling: 'merge',
    });
  }

  closeSearch(): void {
    void this.router.navigate([...exploreTabRoute(this.key(), 'sweep')], {
      queryParams: { [EXPLORE_SEARCH_PARAM]: null },
      queryParamsHandling: 'merge',
    });
    void this.history()?.refresh();
  }

  setTab(tab: string | number | undefined): void {
    if (tab === 'new' || tab === 'history') this.activeTab.set(tab);
  }
}

import { ChangeDetectionStrategy, Component, computed, inject, input, signal, viewChild } from '@angular/core';
import { Tab, TabList, TabPanel, TabPanels, Tabs } from 'primeng/tabs';

import { WalkForwardStudyFormComponent, type WalkForwardStudyLaunch } from '../../../components/walk-forward-study/walk-forward-study-form.component';
import { WalkForwardStudyHistoryComponent } from '../../../components/walk-forward-study/walk-forward-study-history.component';
import { WalkForwardStudyResultComponent } from '../../../components/walk-forward-study/walk-forward-study-result.component';
import { WalkForwardStudyService } from '../../../components/walk-forward-study/walk-forward-study.service';
import type { WorkspaceStateContent } from '../../../shared/workspace/workspace-state';
import { WorkspaceStateBlockComponent } from '../../../shared/workspace/workspace-state-block.component';
import { ExploreSession } from '../explore-session';
import { goldenTabRoute } from '../../golden/golden-tabs';
import { injectBacktestHandoff } from '../handoff/backtest-handoff';
import { ExploreHandoffNoticeComponent } from '../handoff/explore-handoff-notice.component';
import { injectSweepHandoff } from '../handoff/sweep-handoff';

type WalkForwardTab = 'new' | 'history';

/**
 * Explore's Walk-forward (PRD #1925): configure and launch a walk-forward study
 * for this strategy, and its history. `?search=` fills the form from a Grid
 * Search (the "test this grid out of sample" handoff) and `?run=` from a saved
 * backtest; either only fills the form in, with what arrived and what did not
 * listed above it. The owner chooses the training and test lengths and launches.
 * A launched study opens in History as soon as its record is listable, which the
 * jobs boundary guarantees before it returns the job id.
 */
@Component({
  selector: 'app-explore-walk-forward',
  imports: [
    Tabs,
    TabList,
    Tab,
    TabPanels,
    TabPanel,
    ExploreHandoffNoticeComponent,
    WalkForwardStudyFormComponent,
    WalkForwardStudyHistoryComponent,
    WalkForwardStudyResultComponent,
    WorkspaceStateBlockComponent,
  ],
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './explore-walk-forward.component.html',
  styleUrl: './explore-walk-forward.component.scss',
})
export class ExploreWalkForwardComponent {
  private readonly service = inject(WalkForwardStudyService);
  private readonly session = inject(ExploreSession);

  /** The saved Grid Search the form starts from (`?search=`). */
  readonly search = input<string | undefined>(undefined);
  /** The saved backtest the form starts from (`?run=`). */
  readonly run = input<string | undefined>(undefined);

  protected readonly key = this.session.key;
  /** Golden plans a search from a study's grid, where the registry allows a golden search at all. */
  protected readonly goldenRoute = computed(() => (this.session.goldenCapability()?.supported === true ? goldenTabRoute(this.key(), 'studies') : null));

  readonly activeTab = signal<WalkForwardTab>('new');
  readonly openStudyId = signal<string | null>(null);
  readonly lookupMessage = signal<string | null>(null);

  private readonly history = viewChild(WalkForwardStudyHistoryComponent);

  /** Strategy Lab's entry for this strategy, once it has been read. */
  protected readonly strategy = computed(() => {
    const catalog = this.session.catalog();
    return catalog.kind === 'ready' ? catalog.value : null;
  });
  /** The same entry as a list, for the result and history views that name strategies. */
  protected readonly strategies = computed(() => {
    const strategy = this.strategy();
    return strategy === null ? [] : [strategy];
  });

  private readonly fromSweep = injectSweepHandoff(this.search);
  private readonly fromBacktest = injectBacktestHandoff('walk-forward', this.run);
  /** A search in the address wins over a run: it already is a grid. */
  protected readonly handoff = computed(() => this.fromSweep() ?? this.fromBacktest());
  protected readonly seed = computed(() => {
    const state = this.handoff();
    return state?.kind === 'ready' ? state.handoff.spec : null;
  });

  /** Why the tool cannot be used at all for this strategy, or `null`. */
  protected readonly blocked = computed<WorkspaceStateContent | null>(() => {
    const capability = this.session.sweepCapability();
    if (capability !== null && !capability.supported) {
      return {
        kind: 'unavailable',
        happened: 'The registry does not allow a walk-forward of this strategy.',
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
        next: 'A walk-forward needs them to build its grid.',
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
  }

  onAction(actionId: string): void {
    if (actionId === 'retry') this.session.retryCatalog();
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
}

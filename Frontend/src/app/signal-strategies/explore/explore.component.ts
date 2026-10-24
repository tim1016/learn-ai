import { ChangeDetectionStrategy, Component, computed, inject } from '@angular/core';
import { RouterOutlet } from '@angular/router';

import { StrategyLabRunner } from '../../components/strategy-lab/strategy-lab-runner.service';
import { HowThisWorksLinkComponent } from '../../shared/help/how-this-works-link.component';
import { WorkspaceTabListComponent, type WorkspaceTabItem } from '../../shared/workspace/workspace-tab-list.component';
import { CurrentUrlService } from '../../shell/current-url.service';
import { ExploreSession } from './explore-session';
import { EXPLORE_RUN_PARAM, EXPLORE_TABS, exploreTabOf, exploreTabRoute } from './explore-tabs';

/**
 * A strategy's Explore (PRD #3490 R17): Backtest, Sweep and Walk-forward, each
 * with its own history, and a statement of what they are for. Nothing in
 * Explore approves settings; Golden is where that happens.
 *
 * The strip is this page's, and the tools below it are routed children. The
 * state they share lives in the strategy's Explore scope, above this page, so
 * the strip can show that a backtest is still running while another tool is
 * open, and lead back to the run that tool last showed.
 */
@Component({
  selector: 'app-explore',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [HowThisWorksLinkComponent, RouterOutlet, WorkspaceTabListComponent],
  templateUrl: './explore.component.html',
  styleUrl: './explore.component.scss',
})
export class ExploreComponent {
  private readonly session = inject(ExploreSession);
  private readonly runner = inject(StrategyLabRunner);
  private readonly currentUrl = inject(CurrentUrlService).url;

  protected readonly activeTab = computed(() => exploreTabOf(this.currentUrl()));

  protected readonly tabs = computed<readonly WorkspaceTabItem[]>(() => {
    const key = this.session.key();
    const sweep = this.session.sweepCapability();
    const pending = this.runner.pendingOpenRunId();
    const lastRun = pending ?? this.session.backtestRunId();
    return EXPLORE_TABS.tabs.map((tab): WorkspaceTabItem => {
      if (tab.id === 'backtest') {
        return {
          ...tab,
          route: exploreTabRoute(key, tab.id),
          queryParams: lastRun === null ? undefined : { [EXPLORE_RUN_PARAM]: String(lastRun) },
          badge: this.runner.running() ? 'running' : pending !== null ? 'run ready' : undefined,
        };
      }
      // Sweep and Walk-forward share one registry answer.
      return sweep !== null && !sweep.supported
        ? { ...tab, route: null, unavailableReason: sweep.reason ?? 'The registry does not allow it for this strategy.' }
        : { ...tab, route: exploreTabRoute(key, tab.id) };
    });
  });

  /** Tools the registry rules out for this strategy, with its reason where the reader can see it. */
  protected readonly unavailable = computed(() =>
    this.tabs().flatMap((tab) => (tab.unavailableReason === undefined ? [] : [{ id: tab.id, label: tab.label, reason: tab.unavailableReason }])),
  );
}

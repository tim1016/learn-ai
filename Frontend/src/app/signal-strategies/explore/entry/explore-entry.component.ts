import { ChangeDetectionStrategy, Component, computed, input } from '@angular/core';
import { RouterLink } from '@angular/router';

import type { WorkspaceAction } from '../../../shared/workspace/workspace-action';
import { WorkspaceHeaderComponent, type WorkspaceBreadcrumbItem } from '../../../shared/workspace/workspace-header.component';
import type { WorkspaceStateContent } from '../../../shared/workspace/workspace-state';
import { WorkspaceStateBlockComponent } from '../../../shared/workspace/workspace-state-block.component';
import { SignalStrategyChipComponent } from '../../signal-strategy-chip.component';
import { SIGNAL_STRATEGIES_ROUTE } from '../../signal-strategy-tabs';
import { EXPLORE_RUN_PARAM, EXPLORE_SEARCH_PARAM, EXPLORE_TABS, exploreTabRoute, type ExploreTabId } from '../explore-tabs';
import type { ExploreEntry, ExploreEntryRefusal } from './explore-entry';
import { UnregisteredRunReportComponent } from './unregistered-run-report.component';

const TOOL_WORDS: Readonly<Record<ExploreTabId, string>> = {
  backtest: 'backtest',
  sweep: 'sweep',
  'walk-forward': 'walk-forward',
};

const TO_LIST: WorkspaceAction = { id: 'list', label: 'Back to Signal strategies', route: [SIGNAL_STRATEGIES_ROUTE] };

function refusalState(refusal: ExploreEntryRefusal, target: ExploreTabId): WorkspaceStateContent {
  switch (refusal.reason) {
    case 'invalid-run':
      return {
        kind: 'invalid',
        happened: `"${refusal.raw}" is not a saved run's number.`,
        why: 'A run is named by a whole number above zero.',
        next: 'Check the link, or choose a strategy and open its backtests.',
        actions: [TO_LIST],
      };
    case 'run-not-found':
      return {
        kind: 'unavailable',
        happened: `Saved run #${refusal.runId} was not found.`,
        why: 'The server holds no backtest run with that number, so no strategy can be named for it.',
        next: 'Check the number, or choose a strategy and open its backtests.',
        actions: [TO_LIST],
      };
    case 'run-mismatch':
      return {
        kind: 'unavailable',
        happened: `Saved run #${refusal.runId} belongs to ${refusal.owner}, but the link asked for ${refusal.claimed}.`,
        why: 'A run opens only under the strategy that produced it.',
        next: `Open it under ${refusal.owner}.`,
        actions: [
          {
            id: 'owner',
            label: `Open run #${refusal.runId} under ${refusal.owner}`,
            route: exploreTabRoute(refusal.owner, 'backtest'),
            queryParams: { [EXPLORE_RUN_PARAM]: String(refusal.runId) },
          },
          TO_LIST,
        ],
      };
    case 'search-not-found':
      return {
        kind: 'unavailable',
        happened: `Grid search ${refusal.searchId} was not found.`,
        why: 'The server holds no search with that id, so no strategy can be named for it.',
        next: 'Check the link, or choose a strategy and open its sweeps.',
        actions: [TO_LIST],
      };
    case 'search-unresolved':
      return {
        kind: 'unavailable',
        happened: `Grid search ${refusal.searchId.slice(0, 8)} stored the strategy "${refusal.storedStrategy}", which the registry does not list.`,
        why: 'No strategy is guessed for it, and the record is unchanged.',
        next: 'Choose a registered strategy to explore.',
        actions: [TO_LIST],
      };
    case 'search-mismatch':
      return {
        kind: 'unavailable',
        happened: `Grid search ${refusal.searchId.slice(0, 8)} belongs to ${refusal.owner}, but the link asked for ${refusal.claimed}.`,
        why: 'A search opens only under the strategy that ran it.',
        next: `Open it under ${refusal.owner}.`,
        actions: [
          {
            id: 'owner',
            label: `Open it under ${refusal.owner}`,
            route: exploreTabRoute(refusal.owner, target === 'walk-forward' ? 'walk-forward' : 'sweep'),
            queryParams: { [EXPLORE_SEARCH_PARAM]: refusal.searchId },
          },
          TO_LIST,
        ],
      };
    case 'read-failed':
      return {
        kind: 'failed',
        happened: `${refusal.subject.charAt(0).toUpperCase()}${refusal.subject.slice(1)} could not be read.`,
        why: refusal.message,
        next: 'Without it the owning strategy cannot be named. Reload to try again.',
        actions: [TO_LIST],
      };
  }
}

/**
 * Where an old Strategy Lab, Grid Search or Walk-forward address, or an evidence
 * address, lands when its owner cannot be named: a chooser of the registered
 * strategies when the address named none, or the reason in words when the
 * stored evidence did not resolve. It stays at the address it was opened at,
 * keeping the id, and never opens a strategy the evidence did not name. A saved
 * run no registered strategy holds is the one record it still shows: by itself,
 * read-only.
 */
@Component({
  selector: 'app-explore-entry',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [
    RouterLink,
    SignalStrategyChipComponent,
    UnregisteredRunReportComponent,
    WorkspaceHeaderComponent,
    WorkspaceStateBlockComponent,
  ],
  templateUrl: './explore-entry.component.html',
  styleUrl: './explore-entry.component.scss',
})
export class ExploreEntryComponent {
  /** What the route's resolver found. */
  readonly entry = input.required<ExploreEntry>();

  protected readonly title = computed(() => {
    const entry = this.entry();
    if (entry.kind === 'run-report') return `Saved run #${entry.runId}`;
    return entry.kind === 'choose' ? `Choose a strategy to ${TOOL_WORDS[entry.target]}` : EXPLORE_TABS.label(entry.target);
  });

  protected readonly breadcrumb = computed<readonly WorkspaceBreadcrumbItem[]>(() => [
    { label: 'Signal strategies', route: [SIGNAL_STRATEGIES_ROUTE] },
    { label: this.entry().kind === 'run-report' ? 'Saved run' : 'Explore' },
  ]);

  /** The run no registered strategy holds, when that is what the address named. */
  protected readonly savedRun = computed(() => {
    const entry = this.entry();
    return entry.kind === 'run-report' ? entry : null;
  });

  protected readonly refusal = computed<WorkspaceStateContent | null>(() => {
    const entry = this.entry();
    return entry.kind === 'refused' ? refusalState(entry.refusal, entry.target) : null;
  });

  /** The registered strategies, each with whether the chosen tool is open to it. */
  protected readonly choices = computed(() => {
    const entry = this.entry();
    if (entry.kind !== 'choose') return [];
    const sweeping = entry.target !== 'backtest';
    return entry.strategies
      .filter((strategy) => strategy.catalog_visible)
      .map((strategy) => {
        const capability = strategy.capabilities.sweep;
        return {
          key: strategy.strategy_key,
          name: strategy.display_name,
          route: exploreTabRoute(strategy.strategy_key, entry.target),
          query: entry.query,
          closedReason: sweeping && !capability.supported ? (capability.reason ?? 'The registry does not allow it.') : null,
        };
      });
  });

  protected readonly unknownStrategy = computed(() => {
    const entry = this.entry();
    return entry.kind === 'choose' ? entry.unknownStrategy : null;
  });
}

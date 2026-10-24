import { defineWorkspaceTabs } from '../../shared/workspace/workspace-tabs';
import { SIGNAL_STRATEGIES_ROUTE } from '../signal-strategy-tabs';

/**
 * Explore's three tools, declared once (ADR 0084): a strategy's address is
 * `/signal-strategies/:key/explore/(backtest|sweep|walk-forward)`, and each tool
 * reads its own native id from the query (`?run=` a saved backtest, `?search=`
 * a Grid Search).
 */
export type ExploreTabId = 'backtest' | 'sweep' | 'walk-forward';

export const EXPLORE_TABS = defineWorkspaceTabs<ExploreTabId>(
  { backtest: 'Backtest', sweep: 'Sweep', 'walk-forward': 'Walk-forward' },
  ['backtest', 'sweep', 'walk-forward'],
);

/** The query keys Explore's tools read for the evidence they open or start from. */
export const EXPLORE_RUN_PARAM = 'run';
export const EXPLORE_SEARCH_PARAM = 'search';

/** Router commands for one tool of one strategy's Explore. */
export function exploreTabRoute(strategyKey: string, tab: ExploreTabId): readonly string[] {
  return [SIGNAL_STRATEGIES_ROUTE, strategyKey, 'explore', tab];
}

/** The Explore tool a URL names, or `null` for any other address. */
export function exploreTabOf(url: string): ExploreTabId | null {
  const path = url.split(/[?#]/)[0];
  const [root, , workspaceTab, tool] = path.split('/').filter((segment) => segment !== '');
  if (`/${root}` !== SIGNAL_STRATEGIES_ROUTE || workspaceTab !== 'explore') return null;
  return EXPLORE_TABS.tabs.find((tab) => tab.id === tool)?.id ?? null;
}

/** A saved run's id as the query writes it, or `null` for anything that is not a whole number above zero. */
export function parseRunId(raw: string | undefined | null): number | null {
  const parsed = Number(raw);
  return raw !== undefined && raw !== null && raw !== '' && Number.isInteger(parsed) && parsed > 0 ? parsed : null;
}

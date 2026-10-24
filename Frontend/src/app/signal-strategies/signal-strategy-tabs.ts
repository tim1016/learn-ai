import { defineWorkspaceTabs } from '../shared/workspace/workspace-tabs';

/**
 * The signal strategy place's addresses and tabs, declared once (ADR 0084).
 *
 * A strategy is one registry key; its address always carries a tab, so a bare
 * second segment never names one. `recency` and `evidence` are the list's own
 * pages and are never taken for a key.
 */
export const SIGNAL_STRATEGIES_ROUTE = '/signal-strategies';
export const SIGNAL_STRATEGIES_RECENCY_ROUTE = `${SIGNAL_STRATEGIES_ROUTE}/recency`;

/**
 * A saved backtest run's own address. It resolves the run's owner from the
 * record: a registered strategy's Explore when there is one, and otherwise the
 * run itself, read-only, with no strategy around it.
 */
export function savedRunRoute(runId: number): readonly string[] {
  return [SIGNAL_STRATEGIES_ROUTE, 'evidence', 'run', String(runId)];
}

/** Second segments that name a page of the list, not a strategy. */
const RESERVED_SEGMENTS: ReadonlySet<string> = new Set(['recency', 'evidence']);

/** Whether a second segment can be a registry key: it names no page of the list. */
export function canBeStrategyKey(segment: string): boolean {
  return !RESERVED_SEGMENTS.has(segment);
}

export type SignalStrategyTabId = 'overview' | 'define' | 'explore' | 'golden' | 'proof';

export const SIGNAL_STRATEGY_TABS = defineWorkspaceTabs<SignalStrategyTabId>(
  { overview: 'Overview', define: 'Define', explore: 'Explore', golden: 'Golden', proof: 'Proof' },
  ['overview', 'define', 'explore', 'golden', 'proof'],
);

/** Router commands for one strategy's tab. */
export function signalStrategyTabRoute(strategyKey: string, tab: SignalStrategyTabId): readonly string[] {
  return [SIGNAL_STRATEGIES_ROUTE, strategyKey, tab];
}

export interface SignalStrategyLocation {
  readonly strategyKey: string;
  /** `null` when the URL's last segment is not a declared tab. */
  readonly tab: SignalStrategyTabId | null;
}

function tabIdOf(segment: string | undefined): SignalStrategyTabId | null {
  return SIGNAL_STRATEGY_TABS.tabs.find((tab) => tab.id === segment)?.id ?? null;
}

/** The strategy and tab a URL names, or `null` for any other address. */
export function signalStrategyLocation(url: string): SignalStrategyLocation | null {
  const path = url.split(/[?#]/)[0];
  const [root, key, tab] = path.split('/').filter((segment) => segment !== '');
  if (`/${root}` !== SIGNAL_STRATEGIES_ROUTE || key === undefined || !canBeStrategyKey(key)) return null;
  return { strategyKey: decodeURIComponent(key), tab: tabIdOf(tab) };
}

/** The window title of a strategy's page: the entity, its tab, then the group (PRD #3490 R01). */
export function signalStrategyWindowTitle(location: SignalStrategyLocation): string {
  const group = 'Signal strategies';
  return location.tab === null
    ? `${location.strategyKey} · ${group}`
    : `${location.strategyKey} · ${SIGNAL_STRATEGY_TABS.label(location.tab)} · ${group}`;
}

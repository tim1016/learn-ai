import { routePathOf } from '../../fleet/account-workspace';
import { defineWorkspaceTabs } from '../../shared/workspace/workspace-tabs';
import { operationsTabOf, operationsTabRoute } from '../operations-tabs';

/**
 * The checks Operations → Data checks hosts (ADR 0084):
 * `/operations/data-checks/<check>`. A check's id is its URL segment.
 *
 * One place declares them, as `operations-tabs.ts` does for the tabs: the
 * strip, the routes and every redirect that leads here read this list.
 */
export type DataCheck = 'tradingview' | 'divergence' | 'pre-flight';

const CHECKS = defineWorkspaceTabs<DataCheck>(
  {
    tradingview: 'TradingView comparison',
    divergence: 'Data divergence',
    'pre-flight': 'Strategy pre-flight',
  },
  ['tradingview', 'divergence', 'pre-flight'],
);

/** The strip's checks, in the order they are presented. */
export const DATA_CHECKS = CHECKS.tabs;

/** Router commands for one check. */
export function dataCheckRoute(check: DataCheck): readonly string[] {
  return [...operationsTabRoute('data-checks'), check];
}

/** The check a URL opens, or `null` when it is not one of Data checks' checks. */
export function dataCheckOf(url: string): DataCheck | null {
  if (operationsTabOf(url) !== 'data-checks') return null;
  const segment = routePathOf(url).split('/')[3];
  return DATA_CHECKS.find((check) => check.id === segment)?.id ?? null;
}

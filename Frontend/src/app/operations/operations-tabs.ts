import { routePathOf } from '../fleet/account-workspace';
import { defineWorkspaceTabs } from '../shared/workspace/workspace-tabs';

/**
 * Operations' URL vocabulary (ADR 0084): `/operations/<tab>`.
 *
 * One place declares the tabs, as the account workspace does: the strip, the
 * menu (and so the breadcrumb and the window title) and the health notices all
 * read this list, so a tab cannot pick up a second name. A tab's id is its URL
 * segment.
 */
export type OperationsTab = 'lake' | 'option-archive' | 'backups' | 'math-checks' | 'data-checks';

const TABS = defineWorkspaceTabs<OperationsTab>(
  {
    lake: 'Lake',
    'option-archive': 'Option archive',
    backups: 'Backups',
    'math-checks': 'Math checks',
    'data-checks': 'Data checks',
  },
  ['lake', 'option-archive', 'backups', 'math-checks', 'data-checks'],
);

/** The strip's tabs, in the order they are presented. */
export const OPERATIONS_TABS = TABS.tabs;

export const operationsTabLabel = TABS.label;

/** The place's root address; a bare visit redirects to the first tab. */
export const OPERATIONS_ROOT = '/operations';

/** Router commands for one tab. */
export function operationsTabRoute(tab: OperationsTab): readonly string[] {
  return [OPERATIONS_ROOT, tab];
}

/** The tab a URL opens, or `null` when it is not one of Operations' tabs. */
export function operationsTabOf(url: string): OperationsTab | null {
  const [, root, segment] = routePathOf(url).split('/');
  if (`/${root}` !== OPERATIONS_ROOT) return null;
  return OPERATIONS_TABS.find((tab) => tab.id === segment)?.id ?? null;
}

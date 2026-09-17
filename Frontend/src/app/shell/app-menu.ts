import type { MenuItem } from 'primeng/api';

import { accountWorkspaceLocation } from '../fleet/account-workspace';

export interface AppMenuItem {
  /** The single display name for every navigation projection. */
  readonly title: string;
  /** Router path for navigation, and the path the item highlights on. */
  readonly route: string;
}

export interface AppMenuGroup {
  readonly id: string;
  readonly title: string;
  /** PrimeIcons class, e.g. `pi pi-chart-line`. */
  readonly icon: string;
  /** Non-empty: every group trigger opens onto at least one destination. */
  readonly items: readonly [AppMenuItem, ...AppMenuItem[]];
}

export interface ActiveMenuNode {
  readonly group: AppMenuGroup;
  readonly item: AppMenuItem;
  /** The route key the menubar uses for its active highlight. */
  readonly activePath: string;
}

/** Applied to the menubar trigger whose group owns the current route. */
export const ACTIVE_GROUP_CLASS = 'app-menubar-group--active';

/** Applied to the menu entry that is the current route. */
export const ACTIVE_ITEM_CLASS = 'app-menubar-item--active';

/**
 * Canonical application information architecture.
 *
 * Every shell navigation surface projects from this ordered menu. Retired
 * IBKR navigation remains redirect-only in the route table and is
 * intentionally absent here.
 */
export const APP_MENU: readonly AppMenuGroup[] = [
  {
    id: 'data-lab',
    title: 'Stocks',
    icon: 'pi pi-chart-line',
    items: [
      { title: 'Stocks', route: '/data-lab' },
      { title: 'Return Distribution', route: '/data-lab/returns' },
      { title: 'Data Lake Observatory', route: '/data-lake' },
    ],
  },
  {
    id: 'options',
    title: 'Options',
    icon: 'pi pi-sliders-h',
    items: [
      { title: 'Options Lab', route: '/options-lab' },
      { title: 'Options Chain (Live)', route: '/broker/options-chain' },
      { title: 'Options Surface (3D)', route: '/broker/options-surface' },
      { title: 'Pricing Lab', route: '/pricing-lab' },
    ],
  },
  {
    id: 'research',
    title: 'Research',
    icon: 'pi pi-compass',
    items: [
      { title: 'Research Lab', route: '/research-lab' },
      { title: 'Golden Fixtures', route: '/golden-fixtures' },
      { title: 'Edge Analysis', route: '/edge' },
      { title: 'Realized vs IV', route: '/edge/realized-vs-iv' },
      { title: 'Cross-Asset', route: '/edge/cross-asset' },
      { title: 'Regimes', route: '/edge/regimes' },
    ],
  },
  {
    id: 'alpaca',
    title: 'Alpaca',
    icon: 'pi pi-link',
    // One way in (ADR 0064 Decision 2): an account is a place, and Deploy,
    // the bot roster and the Gallery are things one does *on* an account, so
    // they are that account's workspace tabs rather than four broker-wide
    // entries that would each have to ask which account they meant.
    items: [{ title: 'Accounts', route: '/brokers/alpaca' }],
  },
  {
    id: 'strategy-tools',
    title: 'Strategy Tools',
    icon: 'pi pi-briefcase',
    items: [
      { title: 'Strategy Validation', route: '/strategy-validation' },
      { title: 'Strategy Spec', route: '/spec-strategy' },
      { title: 'Strategy Lab', route: '/strategy-lab' },
      { title: 'Grid Search', route: '/grid-search' },
      { title: 'Walk-Forward', route: '/walk-forward' },
    ],
  },
  {
    id: 'documentation',
    title: 'Documentation',
    icon: 'pi pi-book',
    items: [
      { title: 'Strategy Docs', route: '/strategy-docs' },
      { title: 'Indicator Reference', route: '/data-lab-docs' },
      { title: 'Pipeline Docs', route: '/data-quality-docs' },
      { title: 'Indicator Reliability', route: '/docs/indicator-reliability-methodology' },
      { title: 'Signal Engine', route: '/docs/signal-engine-methodology' },
      { title: 'Legal Notices', route: '/legal/notices' },
    ],
  },
];

const ACTIVE_MENU_ITEMS = APP_MENU.flatMap((group) =>
  group.items.map((item) => ({ group, item, activePath: item.route })),
).sort((left, right) => right.activePath.length - left.activePath.length);

/**
 * Resolves the single active menu node for a URL.
 *
 * It deliberately owns the menubar's longest-match behavior and the account
 * alias, so every navigation projection agrees with it.
 */
export function activeMenuNodeFor(url: string): ActiveMenuNode | null {
  const path = routePathOf(url);

  // Every account-workspace URL is one account's place (ADR 0064), so every
  // tab of it highlights Accounts — the workspace's own tabs, not the menu,
  // say which page of that account is open. Its Configuration and not-ready
  // tabs and a bot's own page are inside it too (#2186). The workspace's URL
  // shape is owned by `accountWorkspaceLocation`, not re-expressed here, so
  // the menubar and the workspace shell cannot drift apart about what counts
  // as being inside a workspace.
  //
  // `?deploy` needs no case of its own: Deploy is an account's own action,
  // opened as a drawer over its workspace, so a `?deploy` URL is either this
  // workspace or the account list — and both land on Accounts.
  if (accountWorkspaceLocation(url)?.broker === 'alpaca') {
    const workspaceNode = nodeForActivePath('/brokers/alpaca');
    if (workspaceNode !== null) return workspaceNode;
  }

  return ACTIVE_MENU_ITEMS.find(({ activePath }) => path === activePath || path.startsWith(`${activePath}/`)) ?? null;
}

/** Resolves the document title from the same active node the menubar highlights. */
export function pageTitleFor(url: string): string | null {
  return activeMenuNodeFor(url)?.item.title ?? null;
}

/**
 * PrimeNG projection of the canonical menu, rebuilt per URL.
 *
 * ``MenuItem`` carries no URL-driven active concept, so the active group and
 * entry are marked here with the classes the shell stylesheet targets. Every
 * group is a trigger — no group label navigates.
 */
export function menuItemsFor(url: string): MenuItem[] {
  const active = activeMenuNodeFor(url);
  return APP_MENU.map((group) => ({
    label: group.title,
    icon: group.icon,
    styleClass: group === active?.group ? ACTIVE_GROUP_CLASS : undefined,
    items: group.items.map((item) => ({
      label: item.title,
      routerLink: item.route,
      styleClass: item === active?.item ? ACTIVE_ITEM_CLASS : undefined,
    })),
  }));
}

function nodeForActivePath(activePath: string): ActiveMenuNode | null {
  return ACTIVE_MENU_ITEMS.find((node) => node.activePath === activePath) ?? null;
}

/** `url` without its query string or fragment — what an entry matches on. */
function routePathOf(url: string): string {
  return url.split('#')[0].split('?')[0];
}

import type { MenuItem } from 'primeng/api';

import { accountWorkspaceLocation, routePathOf } from '../fleet/account-workspace';
import { HELP_DOCUMENTS } from '../shared/help/help-documents';

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
  /** Non-empty: every group trigger opens onto at least one destination. The first is the group's landing. */
  readonly items: readonly [AppMenuItem, ...AppMenuItem[]];
  /**
   * Pages the group owns that the menu does not list: a detail page, or a tab
   * another place's shell still hosts. Resolved like an item's route, longest
   * match first, so a page under a listed prefix highlights its own group.
   * They shrink as later slices move those pages into the group's workspaces.
   */
  readonly pages?: readonly AppMenuItem[];
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
 * Canonical application information architecture (ADR 0084).
 *
 * Six groups, in this order: Markets, Research, Signal strategies, Options
 * setups, Accounts, Operations. Every shell navigation surface projects from
 * this ordered menu. Retired IBKR navigation remains redirect-only in the
 * route table and is intentionally absent here. Documents are not a group:
 * the top-bar help index lists them (`HELP_DOCUMENTS`) and a page's "How this
 * works" link opens one.
 *
 * Routes keep their current addresses; each page sits under the group the
 * route inventory (PRD #3490 R32) assigns it, and later slices move pages into
 * the groups' workspaces.
 */
export const APP_MENU: readonly AppMenuGroup[] = [
  {
    id: 'markets',
    title: 'Markets',
    icon: 'pi pi-chart-line',
    items: [
      { title: 'Stocks', route: '/data-lab' },
      { title: 'Return Distribution', route: '/data-lab/returns' },
      { title: 'Options Lab', route: '/options-lab' },
      { title: 'Options Chain (Live)', route: '/broker/options-chain' },
      { title: 'Options Surface (3D)', route: '/broker/options-surface' },
      { title: 'Pricing Lab', route: '/pricing-lab' },
    ],
    // Instrument headlines: the news page is a Research Lab tab until the
    // instrument workspace hosts it.
    pages: [{ title: 'Ticker News', route: '/research-lab/market/news' }],
  },
  {
    id: 'research',
    title: 'Research',
    icon: 'pi pi-compass',
    items: [
      { title: 'Research Lab', route: '/research-lab' },
      { title: 'Edge Analysis', route: '/edge' },
      { title: 'Realized vs IV', route: '/edge/realized-vs-iv' },
      { title: 'Cross-Asset', route: '/edge/cross-asset' },
      { title: 'Regimes', route: '/edge/regimes' },
      { title: 'Strategy Spec', route: '/spec-strategy' },
    ],
  },
  {
    id: 'signal-strategies',
    title: 'Signal strategies',
    icon: 'pi pi-briefcase',
    items: [
      { title: 'Signal strategies', route: '/signal-strategies' },
      // These tools sit beside the strategy list until each moves into a strategy's tabs.
      { title: 'Strategy Lab', route: '/strategy-lab' },
      { title: 'Grid Search', route: '/grid-search' },
      { title: 'Walk-Forward', route: '/walk-forward' },
      { title: 'Golden Search', route: '/golden-search' },
      { title: 'Strategy Validation', route: '/strategy-validation' },
    ],
    // The multi-strategy comparison is one link from the list, not a menu entry.
    pages: [{ title: 'Recency Chart', route: '/signal-strategies/recency' }],
  },
  {
    id: 'options-setups',
    title: 'Options setups',
    icon: 'pi pi-sliders-h',
    items: [
      { title: 'Options Backtest', route: '/options-backtest' },
      { title: 'Options Golden Search', route: '/options-golden-search' },
    ],
    pages: [{ title: 'Options evidence', route: '/options-evidence' }],
  },
  {
    id: 'accounts',
    title: 'Accounts',
    icon: 'pi pi-link',
    // One way in (ADR 0064 Decision 2): an account is a place, and Deploy,
    // the bot roster and the Gallery are things one does *on* an account, so
    // they are that account's workspace tabs rather than four broker-wide
    // entries that would each have to ask which account they meant.
    items: [{ title: 'Accounts', route: '/brokers/alpaca' }],
    // A lane that cannot open for the address given fails in place under
    // Accounts too (FR-096).
    pages: [{ title: 'Broker lane unavailable', route: '/brokers' }],
  },
  {
    id: 'operations',
    title: 'Operations',
    icon: 'pi pi-server',
    items: [
      { title: 'Data Lake Observatory', route: '/data-lake' },
      { title: 'Golden Fixtures', route: '/golden-fixtures' },
    ],
    // Data checks: tabs the Data Lab and Research Lab shells still host.
    pages: [
      { title: 'Validate', route: '/data-lab/validate' },
      { title: 'Data Divergence', route: '/research-lab/inspect/data-divergence' },
      { title: 'Strategy Pre-flight', route: '/research-lab/inspect/pre-flight' },
    ],
  },
];

/**
 * The app's landing: where the entry page's "I'm In", the brand link and a
 * lost URL's way back lead. It is the first destination of the first group
 * (Markets), so moving Markets' landing is one edit here.
 */
export const HOME_ROUTE = APP_MENU[0].items[0].route;

/**
 * The menu's name for a route the menu lists or a group owns, so a page can take
 * its title from the entry that leads to it.
 */
export function menuLabelFor(route: string): string {
  const item = APP_MENU.flatMap((group) => [...group.items, ...(group.pages ?? [])]).find((candidate) => candidate.route === route);
  if (item === undefined) throw new Error(`No menu entry lists ${route}.`);
  return item.title;
}

/** Where a group opens: its first item (the group trigger itself never navigates). */
export function groupLanding(group: AppMenuGroup): string {
  return group.items[0].route;
}

const ACTIVE_MENU_NODES = APP_MENU.flatMap((group) =>
  [...group.items, ...(group.pages ?? [])].map((item) => ({ group, item, activePath: item.route })),
).sort((left, right) => right.activePath.length - left.activePath.length);

const HELP_DOCUMENT_ROUTES: ReadonlySet<string> = new Set(HELP_DOCUMENTS.map((document) => document.route));

/**
 * Resolves the single active menu node for a URL.
 *
 * It deliberately owns the menubar's longest-match behavior and the account
 * alias, so every navigation projection agrees with it. A served document has
 * no node: the help index is its home, and a document that sits under a
 * listed prefix (the Strategy Lab manual) must not light up that page's group.
 */
export function activeMenuNodeFor(url: string): ActiveMenuNode | null {
  const path = routePathOf(url);
  if (HELP_DOCUMENT_ROUTES.has(path)) return null;

  // Every account-workspace URL is one account's place (ADR 0064), so every
  // tab of it highlights Accounts — the workspace's own tabs, not the menu,
  // say which page of that account is open. Its Settings and not-ready
  // tabs and a bot's own page are inside it too (#2186). The workspace's URL
  // shape is owned by `accountWorkspaceLocation`, not re-expressed here, so
  // the menubar and the workspace shell cannot drift apart about what counts
  // as being inside a workspace.
  //
  // `?deploy` needs no case of its own: Deploy is an account's own action,
  // opened as a drawer over its workspace, so a `?deploy` URL is either this
  // workspace or the account list — and both land on Accounts.
  if (accountWorkspaceLocation(url)?.broker === 'alpaca') {
    const workspaceNode = ACTIVE_MENU_NODES.find((node) => node.activePath === '/brokers/alpaca');
    if (workspaceNode !== undefined) return workspaceNode;
  }

  return ACTIVE_MENU_NODES.find(({ activePath }) => path === activePath || path.startsWith(`${activePath}/`)) ?? null;
}

/**
 * Resolves the window title for a URL: the page, then its group, as
 * `Page · Group` (PRD #3490 R01). A page named for its group (Accounts) says
 * it once, and a served document is titled by itself.
 */
export function windowTitleFor(url: string): string | null {
  const node = activeMenuNodeFor(url);
  if (node === null) return HELP_DOCUMENTS.find((document) => document.route === routePathOf(url))?.title ?? null;
  return node.item.title === node.group.title ? node.item.title : `${node.item.title} · ${node.group.title}`;
}

/** One step of the way back from a page: where it leads, by the menu's own names. */
export interface MenuCrumb {
  readonly label: string;
  readonly route: string;
}

/**
 * The trail from a page back to its group: the group (landing on its first
 * item), then the listed page it sits inside when its own heading names
 * something deeper. A listed page that is the group's own landing adds no
 * second link to the same place. Empty for a page outside every group.
 */
export function menuTrailFor(url: string, heading: string): readonly MenuCrumb[] {
  const node = activeMenuNodeFor(url);
  if (node === null) return [];
  const landing = groupLanding(node.group);
  const trail: MenuCrumb[] = [{ label: node.group.title, route: landing }];
  if (node.group.items.includes(node.item) && node.item.title !== heading && node.item.route !== landing) {
    trail.push({ label: node.item.title, route: node.item.route });
  }
  return trail;
}

/**
 * PrimeNG projection of the canonical menu, rebuilt per URL.
 *
 * ``MenuItem`` carries no URL-driven active concept, so the active group and
 * entry are marked here with the classes the shell stylesheet targets. Every
 * group is a trigger — no group label navigates. A URL with no page (`null`,
 * or one outside every group) marks nothing.
 */
export function menuItemsFor(url: string | null): MenuItem[] {
  const active = url === null ? null : activeMenuNodeFor(url);
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

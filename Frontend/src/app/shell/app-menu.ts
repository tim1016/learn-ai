export interface AppMenuItem {
  /** The single display name for every navigation projection. */
  readonly title: string;
  /** Router path for navigation. */
  readonly route: string;
  /** Query parameters required to navigate to this menu item. */
  readonly queryParams?: Readonly<Record<string, string>>;
  /** Path used to mark the item active when it differs from its link target. */
  readonly activePath?: string;
}

export interface AppMenuGroup {
  readonly id: string;
  readonly title: string;
  /** PrimeIcons class, e.g. `pi pi-chart-line`. */
  readonly icon: string;
  /** The first item is the group default and is guaranteed to exist. */
  readonly items: readonly [AppMenuItem, ...AppMenuItem[]];
}

export interface ActiveMenuNode {
  readonly group: AppMenuGroup;
  readonly item: AppMenuItem;
  /** The route key the rail uses for its active highlight. */
  readonly activePath: string;
}

export interface Crumb {
  readonly title: string;
  readonly route: string;
  readonly queryParams?: Readonly<Record<string, string>>;
}

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
    title: 'Data Lab',
    icon: 'pi pi-database',
    items: [
      { title: 'Data Lab', route: '/data-lab' },
      { title: 'Indicator Report', route: '/indicator-report' },
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
    ],
  },
  {
    id: 'edge',
    title: 'Edge Analysis',
    icon: 'pi pi-bolt',
    items: [
      { title: 'Overview', route: '/edge' },
      { title: 'Realized vs IV', route: '/edge/realized-vs-iv' },
      { title: 'Cross-Asset', route: '/edge/cross-asset' },
      { title: 'Regimes', route: '/edge/regimes' },
    ],
  },
  {
    id: 'alpaca',
    title: 'Alpaca',
    icon: 'pi pi-link',
    items: [
      { title: 'Accounts', route: '/brokers/alpaca' },
      {
        title: 'Deploy',
        route: '/brokers/alpaca',
        queryParams: { deploy: '' },
        activePath: '/brokers/alpaca/deploy',
      },
      { title: 'Bots', route: '/brokers/alpaca/bots' },
      { title: 'Gallery', route: '/brokers/alpaca/gallery' },
    ],
  },
  {
    id: 'design-lab',
    title: 'Design Lab',
    icon: 'pi pi-palette',
    items: [
      { title: 'Desert Oasis', route: '/broker/desert-oasis' },
      { title: 'Bot Sprites', route: '/broker/bot-sprites' },
    ],
  },
  {
    id: 'strategy-tools',
    title: 'Strategy Tools',
    icon: 'pi pi-briefcase',
    items: [
      { title: 'Strategy Validation', route: '/strategy-validation' },
      { title: 'Strategy Spec', route: '/spec-strategy' },
      { title: 'Strategy Lab', route: '/strategy-lab' },
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
  group.items.map((item) => ({
    group,
    item,
    activePath: item.activePath ?? item.route,
  })),
).sort((left, right) => right.activePath.length - left.activePath.length);

/** Resolves the group default from its ordered, non-empty item list. */
export function defaultMenuItemFor(group: AppMenuGroup): AppMenuItem {
  return group.items[0];
}

/**
 * Resolves the single active menu node for a URL.
 *
 * It deliberately owns the rail's original longest-match behavior and the
 * account/deploy aliases, so every navigation projection agrees with it.
 */
export function activeMenuNodeFor(url: string): ActiveMenuNode | null {
  const { path, query } = splitUrl(url);
  if (path === '/brokers/alpaca' && new URLSearchParams(query).has('deploy')) {
    return nodeForActivePath('/brokers/alpaca/deploy');
  }

  const accountScopedBrokerSurface = path.match(
    /^\/brokers\/([^/]+)\/accounts\/[^/]+\/(deploy|bots|gallery)(?:\/|$)/,
  );
  if (accountScopedBrokerSurface) {
    const [, broker, surface] = accountScopedBrokerSurface;
    const accountNode = nodeForActivePath(`/brokers/${broker.toLowerCase()}/${surface}`);
    if (accountNode !== null) return accountNode;
  }

  return ACTIVE_MENU_ITEMS.find(({ activePath }) => path === activePath || path.startsWith(`${activePath}/`)) ?? null;
}

/** Resolves the page title from the same active node used by the navigation rail. */
export function pageTitleFor(url: string): string | null {
  return activeMenuNodeFor(url)?.item.title ?? null;
}

/** Pure breadcrumb projection of the active menu node. */
export function breadcrumbTrailFor(url: string): readonly Crumb[] {
  const node = activeMenuNodeFor(url);
  if (node === null) return [];

  const groupDefault = defaultMenuItemFor(node.group);
  return [crumbFor(node.group.title, groupDefault)];
}

function nodeForActivePath(activePath: string): ActiveMenuNode | null {
  return ACTIVE_MENU_ITEMS.find((node) => node.activePath === activePath) ?? null;
}

function crumbFor(title: string, item: AppMenuItem): Crumb {
  return item.queryParams === undefined
    ? { title, route: item.route }
    : { title, route: item.route, queryParams: item.queryParams };
}

function splitUrl(url: string): { path: string; query: string } {
  const hashIndex = url.indexOf('#');
  const withoutHash = hashIndex === -1 ? url : url.slice(0, hashIndex);
  const queryIndex = withoutHash.indexOf('?');
  return queryIndex === -1
    ? { path: withoutHash, query: '' }
    : { path: withoutHash.slice(0, queryIndex), query: withoutHash.slice(queryIndex + 1) };
}

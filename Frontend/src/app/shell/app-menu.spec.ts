import {
  ACTIVE_GROUP_CLASS,
  ACTIVE_ITEM_CLASS,
  APP_MENU,
  HOME_ROUTE,
  activeMenuNodeFor,
  groupLanding,
  menuItemsFor,
  windowTitleFor,
} from './app-menu';
import { OPERATIONS_TABS, operationsTabLabel } from '../operations/operations-tabs';
import { HELP_DOCUMENTS } from '../shared/help/help-documents';

describe('app menu projections', () => {
  it('projects the canonical menu: six groups in order, each a trigger, none for documents, Accounts offering only Accounts', () => {
    const groups = menuItemsFor('/markets');

    for (const group of groups) {
      expect.soft(group.routerLink, `keeps the ${group.label} group a trigger, never a destination`).toBeUndefined();
      expect.soft(group.items?.length, `keeps the ${group.label} group's entries`).toBeGreaterThan(0);
    }
    expect(groups.map((group) => group.label), 'projects the six groups in order').toEqual([
      'Markets',
      'Research',
      'Signal strategies',
      'Options setups',
      'Accounts',
      'Operations',
    ]);
    expect(groups.map((group) => group.label), 'leaves documents to the help index').not.toContain('Documentation');
    // ADR 0064 Decision 2: Deploy, the bot roster and the Gallery are things
    // done *on* an account, so they are that account's workspace tabs — four
    // broker-wide entries would each have to ask which account they meant.
    const accounts = groups.find((group) => group.label === 'Accounts');

    expect.soft(accounts?.items?.map((item) => item.label), 'offers Accounts and nothing else').toEqual(['Accounts']);
    expect.soft(accounts?.items?.[0]?.routerLink, 'sends Accounts to /brokers/alpaca').toBe('/brokers/alpaca');
  });

  it('never offers a document as a menu entry', () => {
    const documentRoutes = new Set<string>(HELP_DOCUMENTS.map((document) => document.route));

    const offered = APP_MENU.flatMap((group) => group.items.map((item) => item.route)).filter((route) =>
      documentRoutes.has(route),
    );

    expect(offered).toEqual([]);
  });

  it('gives every route one owner, so no page is listed twice', () => {
    const routes = APP_MENU.flatMap((group) => [...group.items, ...(group.pages ?? [])].map((item) => item.route));

    expect(new Set(routes).size).toBe(routes.length);
  });

  it('lands the app on the first destination of the first group', () => {
    expect(HOME_ROUTE).toBe(groupLanding(APP_MENU[0]));
    expect(APP_MENU[0].title).toBe('Markets');
  });

  it('marks the active group and entry, including Edge pages nested within Research', () => {
    const groups = menuItemsFor('/markets/SPY/options/pricing');
    const markets = groups.find((group) => group.label === 'Markets');

    expect.soft(markets?.styleClass, 'marks the active group').toBe(ACTIVE_GROUP_CLASS);
    expect.soft(markets?.items?.find((item) => item.label === 'Markets')?.styleClass, 'marks the active entry').toBe(ACTIVE_ITEM_CLASS);
    const otherGroupsEntries = groups.filter((group) => group.label !== 'Markets').flatMap((group) => group.items ?? []);
    expect.soft(otherGroupsEntries.filter((item) => item.styleClass !== undefined), 'leaves the other entries unmarked').toEqual([]);
    expect.soft(groups.filter((group) => group.styleClass === ACTIVE_GROUP_CLASS), 'marks exactly one group').toHaveLength(1);

    const edgeGroups = menuItemsFor('/edge/regimes');
    const research = edgeGroups.find((group) => group.label === 'Research');

    expect.soft(edgeGroups.map((group) => group.label), 'nests Edge Analysis within Research instead of a separate top-level group').not.toContain('Edge Analysis');
    expect.soft(research?.items?.find((item) => item.label === 'Edge Analysis')?.routerLink, 'links Edge Analysis from Research').toBe('/edge');
    expect.soft(research?.items?.find((item) => item.label === 'Regimes')?.styleClass, 'marks the Regimes entry active').toBe(ACTIVE_ITEM_CLASS);
    expect.soft(research?.styleClass, 'marks the Research group active for an Edge page').toBe(ACTIVE_GROUP_CLASS);
  });

  it('marks the group but no entry for a page the menu does not list', () => {
    const groups = menuItemsFor('/options-evidence/run/run-7');
    const options = groups.find((group) => group.label === 'Options setups');

    expect(options?.styleClass).toBe(ACTIVE_GROUP_CLASS);
    expect(options?.items?.every((item) => item.styleClass === undefined)).toBe(true);
  });

  it('marks nothing for a URL no page answers', () => {
    expect(menuItemsFor(null).every((group) => group.styleClass === undefined)).toBe(true);
    expect(menuItemsFor('/unlisted-page').every((group) => group.styleClass === undefined)).toBe(true);
  });

  it('places the Operations tabs in Operations', () => {
    const operations = menuItemsFor('/operations/backups').find((group) => group.label === 'Operations');

    expect.soft(
      operations?.items?.map((item) => [item.label, item.routerLink]),
      'lists every Operations tab',
    ).toEqual(OPERATIONS_TABS.map((tab) => [tab.label, `/operations/${tab.id}`]));
    expect.soft(
      operations?.items?.find((item) => item.label === operationsTabLabel('backups'))?.styleClass,
      'marks the open tab active',
    ).toBe(ACTIVE_ITEM_CLASS);
    expect.soft(windowTitleFor('/operations/lake'), 'titles the window for the Lake tab').toBe(`${operationsTabLabel('lake')} · Operations`);
    expect.soft(windowTitleFor('/operations/math-checks'), 'titles the window for the Math checks tab').toBe(`${operationsTabLabel('math-checks')} · Operations`);
    expect.soft(windowTitleFor('/operations/data-checks/pre-flight'), 'titles a check by the tab that holds it').toBe(`${operationsTabLabel('data-checks')} · Operations`);
  });

  it('titles the window Page · Group, and an account page by its group alone', () => {
    expect.soft(windowTitleFor('/options-backtest'), 'Options Backtest sits in Options setups').toBe('Options Backtest · Options setups');
    expect.soft(windowTitleFor('/markets'), 'the Markets list is titled by its menu label, once').toBe('Markets');
    expect.soft(windowTitleFor('/brokers/alpaca?deploy='), 'the account list says Accounts once').toBe('Accounts');
    expect.soft(windowTitleFor('/unlisted-page'), 'a page outside every group has no menu title').toBeNull();
    expect.soft(activeMenuNodeFor('/unlisted-page'), 'highlights nothing for a route outside the menu').toBeNull();
  });

  // Which group a route ends in is the route inventory's to pin (every
  // declared route, direct-loaded). These rows pin the entry the menu lights,
  // where it is not simply the route's own listed name.
  it.each([
    // A shell's children light the shell's entry; an instrument's place, on
    // any tab or sub-page, lights Markets.
    ['/markets/SPY/chart', 'Markets', 'Markets'],
    ['/markets/SPY/chart/news', 'Markets', 'Markets'],
    ['/markets/SPY/data/build', 'Markets', 'Markets'],
    ['/options-lab/strategy-finder', 'Markets', 'Options Lab'],
    ['/research-lab/strategy-runs/run-42', 'Research', 'Research Lab'],
    ['/edge/regimes', 'Research', 'Regimes'],
    ['/golden-search/study-9', 'Signal strategies', 'Golden Search'],
    // The old Strategy Lab address of a saved run is not listed; it ends under its own entry.
    ['/engine/runs/204', 'Signal strategies', 'Strategy Lab'],
    // A strategy's place, and what it hosts, light the list's entry.
    ['/signal-strategies/ema_crossover_signal/overview', 'Signal strategies', 'Signal strategies'],
    ['/signal-strategies/ema_crossover_signal/define', 'Signal strategies', 'Signal strategies'],
    ['/signal-strategies/ema_crossover_signal/explore/sweep', 'Signal strategies', 'Signal strategies'],
    ['/signal-strategies/ema_crossover_signal/golden/studies/study-9', 'Signal strategies', 'Signal strategies'],
    ['/signal-strategies/ema_crossover_signal/golden/configuration', 'Signal strategies', 'Signal strategies'],
    ['/signal-strategies/ema_crossover_signal/proof', 'Signal strategies', 'Signal strategies'],
    ['/signal-strategies/evidence/run/91', 'Signal strategies', 'Signal strategies'],
    ['/options-golden-search/study-9', 'Options setups', 'Options Golden Search'],
    // The analytical builder is an Options Lab tab, filed under Options setups (R32).
    ['/options-lab/strategy-builder', 'Options setups', 'Strategy Builder'],
    ['/options-evidence/run/run-7', 'Options setups', 'Options evidence'],
    // Data checks: each check is a page of the one Data checks tab.
    ['/operations/data-checks/tradingview', 'Operations', 'Data checks'],
    ['/operations/data-checks/divergence', 'Operations', 'Data checks'],
    ['/operations/data-checks/pre-flight', 'Operations', 'Data checks'],
    // ADR 0064: every tab of one account is that account's place, so the
    // menubar names the account list rather than moving between Bots and
    // Gallery entries while the workspace's own tabs already say which page
    // is open. A bot's own page and the lane-scoped tabs are inside it too (#2186).
    ['/brokers/alpaca/clerks/clrk_spec/accounts/PA9/gallery', 'Accounts', 'Accounts'],
    ['/brokers/alpaca/clerks/clrk_spec/accounts/PA9/bots/sid-1', 'Accounts', 'Accounts'],
    ['/brokers/alpaca/clerks/clrk_spec/history', 'Accounts', 'Accounts'],
    // A lane the address cannot prove fails in place under Accounts, not nowhere.
    ['/brokers/ibkr/clerks/clrk_other/settings', 'Accounts', 'Broker lane unavailable'],
  ])('resolves %s to its owning group and page: %s > %s', (url, groupTitle, itemTitle) => {
    const node = activeMenuNodeFor(url);

    expect(node?.group.title).toBe(groupTitle);
    expect(node?.item.title).toBe(itemTitle);
  });

  it('leaves a served document without a group, so the page it explains is not lit up for it', () => {
    // The Strategy Lab manual sits under `/strategy-lab`, which is a listed
    // page; it is the help index's, not that page's.
    expect(activeMenuNodeFor('/strategy-lab/docs')).toBeNull();
    expect(menuItemsFor('/strategy-lab/docs').every((group) => group.styleClass === undefined)).toBe(true);
    expect(windowTitleFor('/strategy-lab/docs')).toBe('Strategy Lab Manual');
    expect(windowTitleFor('/legal/notices')).toBe('Legal Notices');
  });

  it.each(HELP_DOCUMENTS.map((document) => [document.title, document.route] as const))(
    'titles the window for the %s document by its own name',
    (title, route) => {
      expect(windowTitleFor(route)).toBe(title);
    },
  );

  it.each([
    '/brokers/alpaca?deploy=',
    '/brokers/alpaca?deploy=&strategy=momentum',
    '/brokers/alpaca/clerks/clrk_spec/accounts/PA9?deploy=',
    '/brokers/alpaca/clerks/clrk_spec/accounts/PA9/bots?deploy=',
    '/brokers/alpaca/clerks/clrk_spec/accounts/PA9/gallery?deploy=',
  ])('keeps Accounts highlighted under a deploy intent at %s', (url) => {
    // Deploy is an account's own action now (#2187) — a drawer over its
    // workspace, opened from the account list's own choice — so `?deploy`
    // needs no menu entry of its own: it is the account list or one account's
    // workspace, and both are Accounts.
    expect(activeMenuNodeFor(url)?.group.title).toBe('Accounts');
    expect(activeMenuNodeFor(url)?.item.title).toBe('Accounts');
    expect(
      menuItemsFor(url).find((group) => group.label === 'Accounts')?.styleClass,
    ).toBe(ACTIVE_GROUP_CLASS);
  });

  it.each([
    '/brokers/alpaca/bots',
    '/brokers/alpaca/gallery',
    '/brokers/alpaca/accounts/PA9/bots',
    '/brokers/alpaca/accounts/PA9/gallery',
  ])('keeps Accounts highlighted on the retired broker-wide surface %s', (url) => {
    // These are redirect-only compatibility URLs now; whatever a bookmark
    // still points at under `/brokers/alpaca/`, the account list is the only
    // Alpaca destination the menu names.
    expect(activeMenuNodeFor(url)?.item.title).toBe('Accounts');
    expect(windowTitleFor(url)).toBe('Accounts');
  });
});

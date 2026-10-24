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
import { HELP_DOCUMENTS } from '../shared/help/help-documents';

describe('app menu projections', () => {
  it('projects the canonical menu: six groups in order, each a trigger, none for documents, Accounts offering only Accounts', () => {
    const groups = menuItemsFor('/data-lab');

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

  it('marks the active group and entry, including demonstrations nested within Research', () => {
    const groups = menuItemsFor('/pricing-lab');
    const markets = groups.find((group) => group.label === 'Markets');

    expect.soft(markets?.styleClass, 'marks the active group').toBe(ACTIVE_GROUP_CLASS);
    expect.soft(markets?.items?.find((item) => item.label === 'Pricing Lab')?.styleClass, 'marks the active entry').toBe(ACTIVE_ITEM_CLASS);
    expect.soft(markets?.items?.find((item) => item.label === 'Options Lab')?.styleClass, 'leaves the other entries unmarked').toBeUndefined();
    expect.soft(groups.filter((group) => group.styleClass === ACTIVE_GROUP_CLASS), 'marks exactly one group').toHaveLength(1);

    const demoGroups = menuItemsFor('/research/demos/regimes');
    const research = demoGroups.find((group) => group.label === 'Research');

    expect.soft(demoGroups.map((group) => group.label), 'nests the demonstrations within Research instead of a separate top-level group').not.toContain('Edge Analysis');
    expect.soft(research?.items?.find((item) => item.label === 'Studies')?.routerLink, 'opens Research on its study list').toBe('/research');
    expect.soft(research?.items?.find((item) => item.label === 'Regimes demo')?.styleClass, 'marks the Regimes demo entry active').toBe(ACTIVE_ITEM_CLASS);
    expect.soft(research?.items?.find((item) => item.label === 'Studies')?.styleClass, 'leaves the list entry unmarked inside a study').toBeUndefined();
    expect.soft(research?.styleClass, 'marks the Research group active for a demonstration').toBe(ACTIVE_GROUP_CLASS);
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

  it('places the Data Lake Observatory in Operations and the Return Distribution study in Markets', () => {
    const lakeEntry = menuItemsFor('/data-lake')
      .find((group) => group.label === 'Operations')
      ?.items?.find((item) => item.label === 'Data Lake Observatory');

    expect.soft(lakeEntry?.routerLink, 'links the Data Lake Observatory entry to /data-lake').toBe('/data-lake');
    expect.soft(lakeEntry?.styleClass, 'marks the Data Lake Observatory entry active').toBe(ACTIVE_ITEM_CLASS);
    expect.soft(windowTitleFor('/data-lake'), 'titles the window for the Data Lake Observatory').toBe('Data Lake Observatory · Operations');

    // The study route is longer than the Stocks entry, so the longest-match
    // resolution must prefer it — the page highlights its own item.
    const returnsEntry = menuItemsFor('/data-lab/returns')
      .find((group) => group.label === 'Markets')
      ?.items?.find((item) => item.label === 'Return Distribution');

    expect.soft(returnsEntry?.routerLink, 'links the Return Distribution entry to /data-lab/returns').toBe('/data-lab/returns');
    expect.soft(returnsEntry?.styleClass, 'marks the Return Distribution entry active').toBe(ACTIVE_ITEM_CLASS);
    expect.soft(activeMenuNodeFor('/data-lab/returns')?.item.route, 'resolves /data-lab/returns to its own menu node').toBe('/data-lab/returns');
    expect.soft(windowTitleFor('/data-lab/returns'), 'titles the window for Return Distribution').toBe('Return Distribution · Markets');
  });

  it('titles the window Page · Group, and an account page by its group alone', () => {
    expect.soft(windowTitleFor('/options-backtest'), 'Options Backtest sits in Options setups').toBe('Options Backtest · Options setups');
    expect.soft(windowTitleFor('/pricing-lab'), 'Pricing Lab sits in Markets').toBe('Pricing Lab · Markets');
    expect.soft(windowTitleFor('/data-lab'), 'the Data Lab route is presented as Stocks').toBe('Stocks · Markets');
    expect.soft(windowTitleFor('/brokers/alpaca?deploy='), 'the account list says Accounts once').toBe('Accounts');
    expect.soft(windowTitleFor('/unlisted-page'), 'a page outside every group has no menu title').toBeNull();
    expect.soft(activeMenuNodeFor('/unlisted-page'), 'highlights nothing for a route outside the menu').toBeNull();
  });

  // Which group a route ends in is the route inventory's to pin (every
  // declared route, direct-loaded). These rows pin the entry the menu lights,
  // where it is not simply the route's own listed name.
  it.each([
    // A shell's children light the shell's entry.
    ['/data-lab/export', 'Markets', 'Stocks'],
    ['/options-lab/chain', 'Markets', 'Options Lab'],
    ['/options-lab/strategy-finder', 'Markets', 'Options Lab'],
    // A spec run is Research evidence at its native address, never a registry strategy's.
    ['/research/history/spec-run/run-42', 'Research', 'History'],
    ['/research', 'Research', 'Studies'],
    ['/research/feature', 'Research', 'Feature study'],
    ['/research/realized-vs-iv', 'Research', 'Realized vs implied vol'],
    ['/research/demos/regimes', 'Research', 'Regimes demo'],
    ['/research/rule-sandbox', 'Research', 'Rule sandbox'],
    ['/golden-search/study-9', 'Signal strategies', 'Golden Search'],
    ['/options-golden-search/study-9', 'Options setups', 'Options Golden Search'],
    // Pages the menu does not list, though another place's shell still hosts them:
    // instrument headlines are Markets', the multi-strategy comparison Signal strategies'.
    ['/research-lab/market/news', 'Markets', 'Ticker News'],
    ['/research-lab/backtests/recency-chart', 'Signal strategies', 'Recency Chart'],
    // The analytical builder is an Options Lab tab, filed under Options setups (R32).
    ['/options-lab/strategy-builder', 'Options setups', 'Strategy Builder'],
    ['/options-evidence/run/run-7', 'Options setups', 'Options evidence'],
    // Data checks: tabs other shells still host.
    ['/data-lab/validate', 'Operations', 'Validate'],
    ['/research-lab/inspect/data-divergence', 'Operations', 'Data Divergence'],
    ['/research-lab/inspect/pre-flight', 'Operations', 'Strategy Pre-flight'],
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

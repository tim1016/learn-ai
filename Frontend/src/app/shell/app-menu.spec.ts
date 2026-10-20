import {
  ACTIVE_GROUP_CLASS,
  ACTIVE_ITEM_CLASS,
  activeMenuNodeFor,
  menuItemsFor,
  pageTitleFor,
  APP_MENU,
} from './app-menu';

describe('app menu projections', () => {
  it('projects the canonical /data-lab menu: every group a trigger, in order, Alpaca offering only Accounts', () => {
    const groups = menuItemsFor('/data-lab');

    for (const group of groups) {
      expect.soft(group.routerLink, `keeps the ${group.label} group a trigger, never a destination`).toBeUndefined();
      expect.soft(group.items?.length, `keeps the ${group.label} group's entries`).toBeGreaterThan(0);
    }
    expect.soft(groups.map((group) => group.label), 'projects the canonical menu in order').toEqual(
      APP_MENU.map((group) => group.title),
    );
    // ADR 0064 Decision 2: Deploy, the bot roster and the Gallery are things
    // done *on* an account, so they are that account's workspace tabs — four
    // broker-wide entries would each have to ask which account they meant.
    const alpaca = groups.find((group) => group.label === 'Alpaca');

    expect.soft(alpaca?.items?.map((item) => item.label), 'offers Alpaca as Accounts and nothing else').toEqual(['Accounts']);
    expect.soft(alpaca?.items?.[0]?.routerLink, 'sends Alpaca Accounts to /brokers/alpaca').toBe('/brokers/alpaca');
  });

  it('marks the active group and entry, including Edge pages nested within Research', () => {
    const groups = menuItemsFor('/pricing-lab');
    const options = groups.find((group) => group.label === 'Options');

    expect.soft(options?.styleClass, 'marks the active group').toBe(ACTIVE_GROUP_CLASS);
    expect.soft(options?.items?.find((item) => item.label === 'Pricing Lab')?.styleClass, 'marks the active entry').toBe(ACTIVE_ITEM_CLASS);
    expect.soft(options?.items?.find((item) => item.label === 'Options Lab')?.styleClass, 'leaves the other entries unmarked').toBeUndefined();
    expect.soft(groups.filter((group) => group.styleClass === ACTIVE_GROUP_CLASS), 'marks exactly one group').toHaveLength(1);

    const edgeGroups = menuItemsFor('/edge/regimes');
    const research = edgeGroups.find((group) => group.label === 'Research');

    expect.soft(edgeGroups.map((group) => group.label), 'nests Edge Analysis within Research instead of a separate top-level group').not.toContain('Edge Analysis');
    expect.soft(research?.items?.find((item) => item.label === 'Edge Analysis')?.routerLink, 'links Edge Analysis from Research').toBe('/edge');
    expect.soft(research?.items?.find((item) => item.label === 'Regimes')?.styleClass, 'marks the Regimes entry active').toBe(ACTIVE_ITEM_CLASS);
    expect.soft(research?.styleClass, 'marks the Research group active for an Edge page').toBe(ACTIVE_GROUP_CLASS);
  });

  it('reaches the Data Lake Observatory and the Return Distribution study from the Stocks group', () => {
    const lakeEntry = menuItemsFor('/data-lake')
      .find((group) => group.label === 'Stocks')
      ?.items?.find((item) => item.label === 'Data Lake Observatory');

    expect.soft(lakeEntry?.routerLink, 'links the Data Lake Observatory entry to /data-lake').toBe('/data-lake');
    expect.soft(lakeEntry?.styleClass, 'marks the Data Lake Observatory entry active').toBe(ACTIVE_ITEM_CLASS);
    expect.soft(pageTitleFor('/data-lake'), 'titles the window for the Data Lake Observatory').toBe('Data Lake Observatory');

    // The study route is longer than the Stocks entry, so the longest-match
    // resolution must prefer it — the page highlights its own item.
    const returnsEntry = menuItemsFor('/data-lab/returns')
      .find((group) => group.label === 'Stocks')
      ?.items?.find((item) => item.label === 'Return Distribution');

    expect.soft(returnsEntry?.routerLink, 'links the Return Distribution entry to /data-lab/returns').toBe('/data-lab/returns');
    expect.soft(returnsEntry?.styleClass, 'marks the Return Distribution entry active').toBe(ACTIVE_ITEM_CLASS);
    expect.soft(activeMenuNodeFor('/data-lab/returns')?.item.route, 'resolves /data-lab/returns to its own menu node').toBe('/data-lab/returns');
    expect.soft(pageTitleFor('/data-lab/returns'), 'titles the window for Return Distribution').toBe('Return Distribution');
  });

  it('resolves routes to their menu node and page title', () => {
    const backtest = activeMenuNodeFor('/options-backtest');

    expect.soft(backtest?.group.title, 'lists Options Backtest beside the other strategy research pages: group').toBe('Strategy Tools');
    expect.soft(backtest?.item.title, 'lists Options Backtest beside the other strategy research pages: item').toBe('Options Backtest');
    expect.soft(pageTitleFor('/options-backtest'), 'titles the window for Options Backtest').toBe('Options Backtest');

    expect.soft(pageTitleFor('/pricing-lab'), 'resolves page titles through the active menu node: Pricing Lab').toBe('Pricing Lab');
    expect.soft(pageTitleFor('/brokers/alpaca/clerks/clrk_spec/accounts/PA9/gallery'), 'resolves page titles through the active menu node: workspace gallery').toBe('Accounts');
    expect.soft(pageTitleFor('/brokers/alpaca?deploy='), 'resolves page titles through the active menu node: deploy intent on the account list').toBe('Accounts');
    expect.soft(pageTitleFor('/brokers/alpaca/clerks/clrk_spec/accounts/PA9?deploy='), 'resolves page titles through the active menu node: deploy intent on a workspace').toBe('Accounts');
    expect.soft(pageTitleFor('/unlisted-page'), 'resolves page titles through the active menu node: unlisted page has none').toBeNull();

    expect.soft(activeMenuNodeFor('/unlisted-page'), 'highlights nothing for a route outside the menu: no node').toBeNull();
    expect.soft(menuItemsFor('/unlisted-page').every((group) => group.styleClass === undefined), 'highlights nothing for a route outside the menu: no group marked').toBe(true);

    expect.soft(pageTitleFor('/data-lab'), 'presents the Data Lab route as Stocks').toBe('Stocks');
    expect.soft(activeMenuNodeFor('/data-lab')?.item.route, 'keeps Data Lake separate: /data-lab resolves to its own route').toBe('/data-lab');
  });

  it.each([
    ['/research-lab/strategy-runs/run-42', 'Research', 'Research Lab'],
    ['/research-lab/walk-forward/wf-42', 'Research', 'Research Lab'],
    ['/research-lab/monte-carlo/mc-42', 'Research', 'Research Lab'],
    ['/research-lab/baselines/baseline-42', 'Research', 'Research Lab'],
    ['/research-lab/signal-report/42', 'Research', 'Research Lab'],
    // ADR 0064: every tab of one account is that account's place, so the
    // menubar names the account list rather than moving between Bots and
    // Gallery entries while the workspace's own tabs already say which page
    // is open.
    ['/brokers/alpaca/clerks/clrk_spec/accounts/PA9', 'Alpaca', 'Accounts'],
    ['/brokers/alpaca/clerks/clrk_spec/accounts/PA9/bots', 'Alpaca', 'Accounts'],
    ['/brokers/alpaca/clerks/clrk_spec/accounts/PA9/gallery', 'Alpaca', 'Accounts'],
    // A bot's own page and the lane-scoped tabs are inside the workspace too
    // (#2186), so the menubar names the account list for them as well.
    ['/brokers/alpaca/clerks/clrk_spec/accounts/PA9/bots/sid-1', 'Alpaca', 'Accounts'],
    ['/brokers/alpaca/clerks/clrk_spec/settings', 'Alpaca', 'Accounts'],
    ['/brokers/alpaca/clerks/clrk_spec/bots', 'Alpaca', 'Accounts'],
    ['/brokers/alpaca/clerks/clrk_spec/gallery', 'Alpaca', 'Accounts'],
  ])('resolves %s to its deepest menu node, %s > %s', (url, groupTitle, itemTitle) => {
    const node = activeMenuNodeFor(url);

    expect(node?.group.title).toBe(groupTitle);
    expect(node?.item.title).toBe(itemTitle);
  });

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
    expect(activeMenuNodeFor(url)?.group.title).toBe('Alpaca');
    expect(activeMenuNodeFor(url)?.item.title).toBe('Accounts');
    expect(
      menuItemsFor(url).find((group) => group.label === 'Alpaca')?.styleClass,
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
    expect(pageTitleFor(url)).toBe('Accounts');
  });
});

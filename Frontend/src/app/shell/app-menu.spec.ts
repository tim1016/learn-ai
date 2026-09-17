import {
  ACTIVE_GROUP_CLASS,
  ACTIVE_ITEM_CLASS,
  activeMenuNodeFor,
  menuItemsFor,
  pageTitleFor,
  APP_MENU,
} from './app-menu';

describe('app menu projections', () => {
  it('keeps every group a trigger, never a destination', () => {
    for (const group of menuItemsFor('/data-lab')) {
      expect(group.routerLink).toBeUndefined();
      expect(group.items?.length).toBeGreaterThan(0);
    }
  });

  it('projects the canonical menu in order', () => {
    expect(menuItemsFor('/data-lab').map((group) => group.label)).toEqual(
      APP_MENU.map((group) => group.title),
    );
  });

  it('marks the active group and the active entry', () => {
    const groups = menuItemsFor('/pricing-lab');
    const options = groups.find((group) => group.label === 'Options');

    expect(options?.styleClass).toBe(ACTIVE_GROUP_CLASS);
    expect(options?.items?.find((item) => item.label === 'Pricing Lab')?.styleClass).toBe(ACTIVE_ITEM_CLASS);
    expect(options?.items?.find((item) => item.label === 'Options Lab')?.styleClass).toBeUndefined();
    expect(groups.filter((group) => group.styleClass === ACTIVE_GROUP_CLASS)).toHaveLength(1);
  });

  it('offers Alpaca as Accounts and nothing else', () => {
    // ADR 0064 Decision 2: Deploy, the bot roster and the Gallery are things
    // done *on* an account, so they are that account's workspace tabs — four
    // broker-wide entries would each have to ask which account they meant.
    const alpaca = menuItemsFor('/data-lab').find((group) => group.label === 'Alpaca');

    expect(alpaca?.items?.map((item) => item.label)).toEqual(['Accounts']);
    expect(alpaca?.items?.[0]?.routerLink).toBe('/brokers/alpaca');
  });

  it.each([
    '/research-lab/strategy-runs/run-42',
    '/research-lab/walk-forward/wf-42',
    '/research-lab/monte-carlo/mc-42',
    '/research-lab/baselines/baseline-42',
    '/research-lab/signal-report/42',
  ])('stops the research-detail route %s at its deepest menu node', (url) => {
    expect(activeMenuNodeFor(url)?.group.title).toBe('Research');
    expect(activeMenuNodeFor(url)?.item.title).toBe('Research Lab');
  });

  it.each([
    '/brokers/alpaca/clerks/clrk_spec/accounts/PA9',
    '/brokers/alpaca/clerks/clrk_spec/accounts/PA9/bots',
    '/brokers/alpaca/clerks/clrk_spec/accounts/PA9/gallery',
    // A bot's own page and the lane-scoped tabs are inside the workspace too
    // (#2186), so the menubar names the account list for them as well.
    '/brokers/alpaca/clerks/clrk_spec/accounts/PA9/bots/sid-1',
    '/brokers/alpaca/clerks/clrk_spec/configuration',
    '/brokers/alpaca/clerks/clrk_spec/bots',
    '/brokers/alpaca/clerks/clrk_spec/gallery',
  ])('highlights Accounts on the workspace URL %s', (url) => {
    // ADR 0064: every tab of one account is that account's place, so the
    // menubar names the account list rather than moving between Bots and
    // Gallery entries while the workspace's own tabs already say which page
    // is open.
    const node = activeMenuNodeFor(url);

    expect(node?.group.title).toBe('Alpaca');
    expect(node?.item.title).toBe('Accounts');
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

  it('resolves page titles through the active menu node', () => {
    expect(pageTitleFor('/pricing-lab')).toBe('Pricing Lab');
    expect(pageTitleFor('/brokers/alpaca/clerks/clrk_spec/accounts/PA9/gallery')).toBe('Accounts');
    expect(pageTitleFor('/brokers/alpaca?deploy=')).toBe('Accounts');
    expect(pageTitleFor('/brokers/alpaca/clerks/clrk_spec/accounts/PA9?deploy=')).toBe('Accounts');
    expect(pageTitleFor('/jobs-demo')).toBeNull();
  });

  it('highlights nothing for a route outside the menu', () => {
    expect(activeMenuNodeFor('/jobs-demo')).toBeNull();
    expect(menuItemsFor('/jobs-demo').every((group) => group.styleClass === undefined)).toBe(true);
  });

  it('reaches the Data Lake Observatory from the Stocks group', () => {
    const entry = menuItemsFor('/data-lake')
      .find((group) => group.label === 'Stocks')
      ?.items?.find((item) => item.label === 'Data Lake Observatory');

    expect(entry?.routerLink).toBe('/data-lake');
    expect(entry?.styleClass).toBe(ACTIVE_ITEM_CLASS);
    expect(pageTitleFor('/data-lake')).toBe('Data Lake Observatory');
  });

  it('presents the Data Lab route as Stocks while keeping Data Lake separate', () => {
    expect(pageTitleFor('/data-lab')).toBe('Stocks');
    expect(activeMenuNodeFor('/data-lab')?.item.route).toBe('/data-lab');
  });

  it('reaches the Return Distribution study from the Stocks group', () => {
    // The study route is longer than the Stocks entry, so the longest-match
    // resolution must prefer it — the page highlights its own item.
    const entry = menuItemsFor('/data-lab/returns')
      .find((group) => group.label === 'Stocks')
      ?.items?.find((item) => item.label === 'Return Distribution');

    expect(entry?.routerLink).toBe('/data-lab/returns');
    expect(entry?.styleClass).toBe(ACTIVE_ITEM_CLASS);
    expect(activeMenuNodeFor('/data-lab/returns')?.item.route).toBe('/data-lab/returns');
    expect(pageTitleFor('/data-lab/returns')).toBe('Return Distribution');
  });

  it('nests Edge Analysis within Research instead of using a separate top-level group', () => {
    const groups = menuItemsFor('/edge/regimes');
    const research = groups.find((group) => group.label === 'Research');

    expect(groups.map((group) => group.label)).not.toContain('Edge Analysis');
    expect(research?.items?.find((item) => item.label === 'Edge Analysis')?.routerLink).toBe('/edge');
    expect(research?.items?.find((item) => item.label === 'Regimes')?.styleClass).toBe(ACTIVE_ITEM_CLASS);
    expect(research?.styleClass).toBe(ACTIVE_GROUP_CLASS);
  });

  it('omits the retired Indicator Report and Design Lab surfaces', () => {
    const groups = menuItemsFor('/data-lab');
    const entries = groups.flatMap((group) => group.items ?? []);

    expect(groups.map((group) => group.label)).not.toContain('Design Lab');
    expect(entries.map((item) => item.label)).not.toContain('Indicator Report');
    expect(entries.map((item) => item.routerLink)).not.toContain('/indicator-report');
  });
});

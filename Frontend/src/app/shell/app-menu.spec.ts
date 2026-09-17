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

  it('carries the query parameters an entry needs to navigate', () => {
    const alpaca = menuItemsFor('/data-lab').find((group) => group.label === 'Alpaca');
    const deploy = alpaca?.items?.find((item) => item.label === 'Deploy');
    const bots = alpaca?.items?.find((item) => item.label === 'Bot rosters');
    const gallery = alpaca?.items?.find((item) => item.label === 'Gallery');

    expect(deploy?.routerLink).toBe('/brokers/alpaca');
    expect(deploy?.queryParams).toEqual({ deploy: '' });
    expect(bots?.routerLink).toBe('/brokers/alpaca/bots');
    expect(bots?.queryParams).toBeUndefined();
    expect(gallery?.routerLink).toBe('/brokers/alpaca/gallery');
    expect(gallery?.queryParams).toBeUndefined();
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

  it('resolves the deploy query alias to its menu entry', () => {
    expect(activeMenuNodeFor('/brokers/alpaca?deploy=')?.item.title).toBe('Deploy');
    expect(
      menuItemsFor('/brokers/alpaca?deploy=').find((group) => group.label === 'Alpaca')?.styleClass,
    ).toBe(ACTIVE_GROUP_CLASS);
  });

  it.each([
    '/brokers/alpaca/clerks/clrk_spec/accounts/PA9?deploy=',
    '/brokers/alpaca/clerks/clrk_spec/accounts/PA9/bots?deploy=',
    '/brokers/alpaca/clerks/clrk_spec/accounts/PA9/gallery?deploy=',
  ])('resolves the deploy query alias to Deploy from any workspace tab %s', (url) => {
    // Fix c6dda7d8 lets the operator open Deploy from wherever they are
    // standing in the workspace, so every tab's `?deploy` URL — not just the
    // Overview tab's bare account root — must resolve to the same entry.
    expect(activeMenuNodeFor(url)?.item.title).toBe('Deploy');
  });

  it.each([
    ['bots', 'Bot rosters'],
    ['gallery', 'Gallery'],
  ] as const)('resolves the %s chooser route to its menu entry', (surface, title) => {
    expect(activeMenuNodeFor(`/brokers/alpaca/${surface}`)?.item.title).toBe(title);
    expect(pageTitleFor(`/brokers/alpaca/${surface}`)).toBe(title);
  });

  it('resolves page titles through the active menu node', () => {
    expect(pageTitleFor('/pricing-lab')).toBe('Pricing Lab');
    expect(pageTitleFor('/brokers/alpaca/clerks/clrk_spec/accounts/PA9/gallery')).toBe('Accounts');
    expect(pageTitleFor('/brokers/alpaca?deploy=')).toBe('Deploy');
    expect(pageTitleFor('/brokers/alpaca/clerks/clrk_spec/accounts/PA9?deploy=')).toBe('Deploy');
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

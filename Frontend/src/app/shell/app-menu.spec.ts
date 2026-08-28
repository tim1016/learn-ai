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
    const deploy = menuItemsFor('/data-lab')
      .find((group) => group.label === 'Alpaca')
      ?.items?.find((item) => item.label === 'Deploy');

    expect(deploy?.routerLink).toBe('/brokers/alpaca');
    expect(deploy?.queryParams).toEqual({ deploy: '' });
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

  it('maps account-scoped Alpaca pages onto their stable menu entry', () => {
    const node = activeMenuNodeFor('/brokers/alpaca/accounts/PA9/gallery/sid-3');

    expect(node?.group.title).toBe('Alpaca');
    expect(node?.item.title).toBe('Gallery');
  });

  it('resolves the deploy query alias to its menu entry', () => {
    expect(activeMenuNodeFor('/brokers/alpaca?deploy=')?.item.title).toBe('Deploy');
    expect(
      menuItemsFor('/brokers/alpaca?deploy=').find((group) => group.label === 'Alpaca')?.styleClass,
    ).toBe(ACTIVE_GROUP_CLASS);
  });

  it('resolves page titles through the active menu node', () => {
    expect(pageTitleFor('/pricing-lab')).toBe('Pricing Lab');
    expect(pageTitleFor('/brokers/alpaca/accounts/PA9/gallery/sid-3')).toBe('Gallery');
    expect(pageTitleFor('/brokers/alpaca?deploy=')).toBe('Deploy');
    expect(pageTitleFor('/jobs-demo')).toBeNull();
  });

  it('highlights nothing for a route outside the menu', () => {
    expect(activeMenuNodeFor('/jobs-demo')).toBeNull();
    expect(menuItemsFor('/jobs-demo').every((group) => group.styleClass === undefined)).toBe(true);
  });

  it('omits the retired Indicator Report and Design Lab surfaces', () => {
    const groups = menuItemsFor('/data-lab');
    const entries = groups.flatMap((group) => group.items ?? []);

    expect(groups.map((group) => group.label)).not.toContain('Design Lab');
    expect(entries.map((item) => item.label)).not.toContain('Indicator Report');
    expect(entries.map((item) => item.routerLink)).not.toContain('/indicator-report');
  });
});

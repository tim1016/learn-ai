import {
  activeMenuNodeFor,
  breadcrumbTrailFor,
  defaultMenuItemFor,
  pageTitleFor,
  APP_MENU,
} from './app-menu';

describe('app menu projections', () => {
  it('uses the configured group default as the parent crumb target', () => {
    expect(defaultMenuItemFor(APP_MENU[1])).toMatchObject({ title: 'Options Lab', route: '/options-lab' });
    expect(breadcrumbTrailFor('/options-lab')).toEqual([
      { title: 'Options', route: '/options-lab' },
    ]);
  });

  it('returns ancestors only, without the current page crumb', () => {
    expect(breadcrumbTrailFor('/pricing-lab')).toEqual([
      { title: 'Options', route: '/options-lab' },
    ]);
  });

  it.each([
    '/research-lab/strategy-runs/run-42',
    '/research-lab/walk-forward/wf-42',
    '/research-lab/monte-carlo/mc-42',
    '/research-lab/baselines/baseline-42',
    '/research-lab/signal-report/42',
  ])('stops the research-detail route %s at its deepest menu node', (url) => {
    expect(breadcrumbTrailFor(url)).toEqual([
      { title: 'Research', route: '/research-lab' },
    ]);
  });

  it('maps account-scoped Alpaca pages onto their stable menu entry', () => {
    expect(breadcrumbTrailFor('/brokers/alpaca/accounts/PA9/gallery/sid-3')).toEqual([
      { title: 'Alpaca', route: '/brokers/alpaca' },
    ]);
  });

  it('resolves the deploy query alias to its page title and parent crumb', () => {
    expect(activeMenuNodeFor('/brokers/alpaca?deploy=')?.item.title).toBe('Deploy');
    expect(breadcrumbTrailFor('/brokers/alpaca?deploy=')).toEqual([
      { title: 'Alpaca', route: '/brokers/alpaca' },
    ]);
  });

  it('resolves page titles through the active menu node', () => {
    expect(pageTitleFor('/pricing-lab')).toBe('Pricing Lab');
    expect(pageTitleFor('/brokers/alpaca/accounts/PA9/gallery/sid-3')).toBe('Gallery');
    expect(pageTitleFor('/brokers/alpaca?deploy=')).toBe('Deploy');
    expect(pageTitleFor('/jobs-demo')).toBeNull();
  });

  it('returns no crumbs for a route outside the menu', () => {
    expect(activeMenuNodeFor('/jobs-demo')).toBeNull();
    expect(breadcrumbTrailFor('/jobs-demo')).toEqual([]);
  });
});

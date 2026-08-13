import {
  activeMenuNodeFor,
  breadcrumbTrailFor,
  defaultMenuItemFor,
  APP_MENU,
} from './app-menu';

describe('app menu projections', () => {
  it('uses the configured group default as the parent crumb target', () => {
    expect(defaultMenuItemFor(APP_MENU[1])).toMatchObject({ label: 'Options Lab', route: '/options-lab' });
    expect(breadcrumbTrailFor('/options-lab')).toEqual([
      { label: 'Options', route: '/options-lab' },
      { label: 'Options Lab', route: '/options-lab' },
    ]);
  });

  it('links a leaf crumb to its own route', () => {
    expect(breadcrumbTrailFor('/pricing-lab')).toEqual([
      { label: 'Options', route: '/options-lab' },
      { label: 'Pricing Lab', route: '/pricing-lab' },
    ]);
  });

  it('stops a deep route at its deepest menu node', () => {
    expect(breadcrumbTrailFor('/research-lab/strategy-runs/run-42')).toEqual([
      { label: 'Research', route: '/research-lab' },
      { label: 'Research Lab', route: '/research-lab' },
    ]);
  });

  it('maps account-scoped Alpaca pages onto their stable menu entry', () => {
    expect(breadcrumbTrailFor('/brokers/alpaca/accounts/PA9/gallery/sid-3')).toEqual([
      { label: 'Alpaca', route: '/brokers/alpaca' },
      { label: 'Gallery', route: '/brokers/alpaca/gallery' },
    ]);
  });

  it('resolves the deploy query alias without losing its navigation query', () => {
    expect(activeMenuNodeFor('/brokers/alpaca?deploy=')?.item.label).toBe('Deploy');
    expect(breadcrumbTrailFor('/brokers/alpaca?deploy=')).toEqual([
      { label: 'Alpaca', route: '/brokers/alpaca' },
      { label: 'Deploy', route: '/brokers/alpaca', queryParams: { deploy: '' } },
    ]);
  });

  it('returns no crumbs for a route outside the menu', () => {
    expect(activeMenuNodeFor('/jobs-demo')).toBeNull();
    expect(breadcrumbTrailFor('/jobs-demo')).toEqual([]);
  });
});

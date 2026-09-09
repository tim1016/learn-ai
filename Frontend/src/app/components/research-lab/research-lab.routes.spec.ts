import { describe, expect, it } from 'vitest';

import { RESEARCH_LAB_NAV } from './research-lab-nav.config';
import { researchLabRoutes } from './research-lab.routes';
import { NewsPageComponent } from './news/news-page.component';
import { RecencyChartPageComponent } from './recency-chart/recency-chart-page.component';

describe('Research Lab route configuration', () => {
  it('registers the Recency Chart page in the Backtests navigation', async () => {
    const shell = researchLabRoutes.find((route) => route.path === '');
    const pageRoute = shell?.children?.find(
      (route) => route.path === 'backtests/recency-chart',
    );
    const backtests = RESEARCH_LAB_NAV.find((group) => group.label === 'Backtests');

    if (pageRoute?.loadComponent === undefined) {
      throw new Error('Recency Chart route is missing.');
    }
    expect(await pageRoute.loadComponent()).toBe(RecencyChartPageComponent);
    expect(pageRoute?.data?.['title']).toBe('Recency Chart');
    expect(backtests?.items).toContainEqual({
      path: 'backtests/recency-chart',
      label: 'Recency Chart',
    });
  });

  it('registers the Ticker News page in the Market navigation', async () => {
    const shell = researchLabRoutes.find((route) => route.path === '');
    const pageRoute = shell?.children?.find((route) => route.path === 'market/news');
    const market = RESEARCH_LAB_NAV.find((group) => group.label === 'Market');

    if (pageRoute?.loadComponent === undefined) {
      throw new Error('Ticker News route is missing.');
    }
    expect(await pageRoute.loadComponent()).toBe(NewsPageComponent);
    expect(pageRoute?.data?.['title']).toBe('Ticker News');
    expect(market?.items).toContainEqual({ path: 'market/news', label: 'Ticker News' });
  });
});

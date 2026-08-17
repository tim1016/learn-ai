import { describe, expect, it } from 'vitest';

import { RESEARCH_LAB_NAV } from './research-lab-nav.config';
import { researchLabRoutes } from './research-lab.routes';
import { SpyEmaWalkForwardPageComponent } from './spy-ema-walk-forward/spy-ema-walk-forward-page.component';
import { RecencyChartPageComponent } from './recency-chart/recency-chart-page.component';

describe('Research Lab route configuration', () => {
  it('registers the SPY EMA walk-forward page in the Backtests navigation', async () => {
    const shell = researchLabRoutes.find((route) => route.path === '');
    const pageRoute = shell?.children?.find(
      (route) => route.path === 'backtests/spy-ema-walk-forward',
    );
    const backtests = RESEARCH_LAB_NAV.find((group) => group.label === 'Backtests');

    if (pageRoute?.loadComponent === undefined) {
      throw new Error('SPY EMA walk-forward route is missing.');
    }
    expect(await pageRoute.loadComponent()).toBe(SpyEmaWalkForwardPageComponent);
    expect(pageRoute?.data?.['title']).toBe('SPY EMA Walk-Forward');
    expect(backtests?.items).toContainEqual({
      path: 'backtests/spy-ema-walk-forward',
      label: 'EMA Walk-Forward',
    });
  });

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
});

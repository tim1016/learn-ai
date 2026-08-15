import { describe, expect, it } from 'vitest';

import { RESEARCH_LAB_NAV } from './research-lab-nav.config';
import { researchLabRoutes } from './research-lab.routes';

describe('Research Lab route configuration', () => {
  it('registers the SPY EMA walk-forward page in the Backtests navigation', () => {
    const shell = researchLabRoutes.find((route) => route.path === '');
    const pageRoute = shell?.children?.find(
      (route) => route.path === 'backtests/spy-ema-walk-forward',
    );
    const backtests = RESEARCH_LAB_NAV.find((group) => group.label === 'Backtests');

    expect(pageRoute?.loadComponent).toBeTypeOf('function');
    expect(pageRoute?.data?.['title']).toBe('SPY EMA Walk-Forward');
    expect(backtests?.items).toContainEqual({
      path: 'backtests/spy-ema-walk-forward',
      label: 'EMA Walk-Forward',
    });
  });
});

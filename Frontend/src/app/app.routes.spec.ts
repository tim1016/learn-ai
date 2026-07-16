import { describe, expect, it } from 'vitest';

import { AccountMonitorRedirectComponent } from './components/broker/account-monitor-redirect/account-monitor-redirect.component';
import { routes } from './app.routes';

describe('routes', () => {
  it('keeps the retired Account Monitor bookmark as the one-time Accounts redirect', async () => {
    const route = routes.find((candidate) => candidate.path === 'broker/account-monitor');
    if (route?.loadComponent === undefined) throw new Error('Account Monitor redirect route is missing.');

    expect(await route.loadComponent()).toBe(AccountMonitorRedirectComponent);
  });
});

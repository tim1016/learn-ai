import { describe, expect, it } from 'vitest';

import { AccountMonitorRedirectComponent } from './components/broker/account-monitor-redirect/account-monitor-redirect.component';
import { BrokerDeployPageComponent } from './components/broker/broker-deploy-page/broker-deploy-page.component';
import { AlpacaBotControlExampleComponent } from './components/examples/alpaca-bot-control/alpaca-bot-control-example.component';
import { routes } from './app.routes';

describe('routes', () => {
  it('uses Strategy Lab as the canonical workbench and redirects legacy Engine Lab paths', () => {
    expect(routes.find((route) => route.path === 'strategy-lab')?.loadComponent).toBeDefined();
    expect(routes.find((route) => route.path === 'strategy-lab/runs/:id')?.loadComponent).toBeDefined();
    for (const path of ['engine', 'lean-engine', 'lean-lab']) {
      expect(routes.find((route) => route.path === path)).toMatchObject({
        redirectTo: 'strategy-lab',
        pathMatch: 'full',
      });
    }
    expect(routes.find((route) => route.path === 'engine/runs/:id')).toMatchObject({
      redirectTo: 'strategy-lab/runs/:id',
      pathMatch: 'full',
    });
  });

  it('uses one Broker Deploy page for legacy and broker-aware routes', async () => {
    const paths = [
      'broker/deploy',
      'brokers/:broker/deploy',
      'brokers/:broker/accounts/:accountId/deploy',
    ];
    for (const path of paths) {
      const route = routes.find((candidate) => candidate.path === path);
      if (route?.loadComponent === undefined) throw new Error(`Deploy route is missing: ${path}`);
      expect(await route.loadComponent()).toBe(BrokerDeployPageComponent);
    }
  });

  it('redirects the retired Broker Status bookmark to the account roster', () => {
    const route = routes.find((candidate) => candidate.path === 'broker');

    expect(route).toMatchObject({ redirectTo: 'broker/accounts', pathMatch: 'full' });
  });

  it('keeps the retired Account Monitor bookmark as the one-time Accounts redirect', async () => {
    const route = routes.find((candidate) => candidate.path === 'broker/account-monitor');
    if (route?.loadComponent === undefined) throw new Error('Account Monitor redirect route is missing.');

    expect(await route.loadComponent()).toBe(AccountMonitorRedirectComponent);
  });

  it('keeps the retired Reconciliation bookmark on Accounts', () => {
    const route = routes.find((candidate) => candidate.path === 'broker/reconciliation');

    expect(route).toMatchObject({ redirectTo: 'broker/accounts', pathMatch: 'full' });
  });

  it.each(['broker/bots', 'broker/bots/:id', 'broker/instances', 'broker/instances/:id'])(
    'redirects the deprecated %s surface to Alpaca Broker V2',
    (path) => {
      const route = routes.find((candidate) => candidate.path === path);

      expect(route).toMatchObject({ redirectTo: 'brokers/alpaca/bots', pathMatch: 'full' });
    },
  );

  it('redirects the deprecated bot manual to the Broker V2 manual', () => {
    const route = routes.find((candidate) => candidate.path === 'broker/bot-manual');

    expect(route).toMatchObject({ redirectTo: 'brokers/alpaca/manual', pathMatch: 'full' });
  });

  it('keeps the Clerk diagnostic gallery unlinked beneath the examples route', async () => {
    const route = routes.find((candidate) => candidate.path === 'examples/alpaca-bot-control');
    if (route?.loadComponent === undefined) throw new Error('Alpaca bot control example route is missing.');

    expect(await route.loadComponent()).toBe(AlpacaBotControlExampleComponent);
  });
});

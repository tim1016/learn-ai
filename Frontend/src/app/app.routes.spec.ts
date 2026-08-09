import { describe, expect, it } from 'vitest';

import { BrokerDeployPageComponent } from './components/broker/broker-deploy-page/broker-deploy-page.component';
import { AlpacaBotControlExampleComponent } from './components/examples/alpaca-bot-control/alpaca-bot-control-example.component';
import { routes } from './app.routes';

describe('routes', () => {
  it('uses one Broker Deploy page for Alpaca Broker V2 routes', async () => {
    const paths = [
      'brokers/:broker/deploy',
      'brokers/:broker/accounts/:accountId/deploy',
    ];
    for (const path of paths) {
      const route = routes.find((candidate) => candidate.path === path);
      if (route?.loadComponent === undefined) throw new Error(`Deploy route is missing: ${path}`);
      expect(await route.loadComponent()).toBe(BrokerDeployPageComponent);
    }
  });

  it.each([
    ['broker', 'brokers/alpaca'],
    ['broker/accounts', 'brokers/alpaca'],
    ['broker/accounts/:accountId', 'brokers/alpaca'],
    ['broker/account-monitor', 'brokers/alpaca'],
    ['broker/reconciliation', 'brokers/alpaca'],
    ['broker/orders', 'brokers/alpaca'],
    ['broker/session-mirror', 'brokers/alpaca'],
    ['broker/paper-run', 'brokers/alpaca/bots'],
    ['broker/instances', 'brokers/alpaca/bots'],
    ['broker/instances/:id', 'brokers/alpaca/bots'],
    ['broker/bots', 'brokers/alpaca/bots'],
    ['broker/bots/:id', 'brokers/alpaca/bots'],
    ['broker/offline-replay', 'brokers/alpaca'],
    ['broker/bot-manual', 'brokers/alpaca/manual'],
    ['broker/deploy', 'brokers/alpaca/deploy'],
  ])('keeps the deprecated %s URL as a redirect to %s', (path, redirectTo) => {
      const route = routes.find((candidate) => candidate.path === path);

      expect(route).toMatchObject({ redirectTo, pathMatch: 'full' });
      expect(route?.loadComponent).toBeUndefined();
  });

  it('keeps the Clerk diagnostic gallery unlinked beneath the examples route', async () => {
    const route = routes.find((candidate) => candidate.path === 'examples/alpaca-bot-control');
    if (route?.loadComponent === undefined) throw new Error('Alpaca bot control example route is missing.');

    expect(await route.loadComponent()).toBe(AlpacaBotControlExampleComponent);
  });
});

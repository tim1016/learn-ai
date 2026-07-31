import { describe, expect, it } from 'vitest';

import { AccountMonitorRedirectComponent } from './components/broker/account-monitor-redirect/account-monitor-redirect.component';
import { BotOperatorManualPageComponent } from './components/broker/bot-operator-manual/bot-operator-manual-page.component';
import { BrokerDeployPageComponent } from './components/broker/broker-deploy-page/broker-deploy-page.component';
import { AlpacaBotControlExampleComponent } from './components/examples/alpaca-bot-control/alpaca-bot-control-example.component';
import { routes } from './app.routes';

describe('routes', () => {
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

  it('lazy-loads the bot operator manual beside the bot fleet', async () => {
    const route = routes.find((candidate) => candidate.path === 'broker/bot-manual');
    if (route?.loadComponent === undefined) throw new Error('Bot manual route is missing.');

    expect(await route.loadComponent()).toBe(BotOperatorManualPageComponent);
  });

  it('keeps the Clerk diagnostic gallery unlinked beneath the examples route', async () => {
    const route = routes.find((candidate) => candidate.path === 'examples/alpaca-bot-control');
    if (route?.loadComponent === undefined) throw new Error('Alpaca bot control example route is missing.');

    expect(await route.loadComponent()).toBe(AlpacaBotControlExampleComponent);
  });
});

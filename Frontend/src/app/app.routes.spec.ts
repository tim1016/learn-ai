import { describe, expect, it } from 'vitest';

import { AccountMonitorRedirectComponent } from './components/broker/account-monitor-redirect/account-monitor-redirect.component';
import { BotOperatorManualPageComponent } from './components/broker/bot-operator-manual/bot-operator-manual-page.component';
import { routes } from './app.routes';

describe('routes', () => {
  it('redirects the retired Broker Status bookmark to the account roster', () => {
    const route = routes.find((candidate) => candidate.path === 'broker');

    expect(route).toMatchObject({ redirectTo: 'broker/accounts', pathMatch: 'full' });
  });

  it('keeps the retired Account Monitor bookmark as the one-time Accounts redirect', async () => {
    const route = routes.find((candidate) => candidate.path === 'broker/account-monitor');
    if (route?.loadComponent === undefined) throw new Error('Account Monitor redirect route is missing.');

    expect(await route.loadComponent()).toBe(AccountMonitorRedirectComponent);
  });

  it('lazy-loads the bot operator manual beside the bot fleet', async () => {
    const route = routes.find((candidate) => candidate.path === 'broker/bot-manual');
    if (route?.loadComponent === undefined) throw new Error('Bot manual route is missing.');

    expect(await route.loadComponent()).toBe(BotOperatorManualPageComponent);
  });
});

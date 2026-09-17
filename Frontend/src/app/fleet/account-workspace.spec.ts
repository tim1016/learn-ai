import { describe, expect, it } from 'vitest';

import {
  accountWorkspaceLocation,
  accountWorkspaceTabRoute,
  type AccountWorkspaceLocation,
} from './account-workspace';

const WORKSPACE = '/brokers/alpaca/clerks/clrk_spec/accounts/PA9';

const LOCATION: AccountWorkspaceLocation = {
  broker: 'alpaca',
  clerkId: 'clrk_spec',
  accountId: 'PA9',
  tab: 'overview',
};

describe('accountWorkspaceLocation', () => {
  it.each([
    [WORKSPACE, 'overview'],
    [`${WORKSPACE}/bots`, 'bots'],
    [`${WORKSPACE}/gallery`, 'gallery'],
  ])('resolves %s to the %s tab', (url, tab) => {
    expect(accountWorkspaceLocation(url)).toEqual({ ...LOCATION, tab });
  });

  it.each([
    [`${WORKSPACE}?lens=operator`, 'a query string'],
    [`${WORKSPACE}#positions`, 'a fragment'],
    [`${WORKSPACE}/`, 'a trailing slash'],
    [`${WORKSPACE}/bots?deploy=`, 'a query string on a tab'],
  ])('resolves %s — %s never hides the workspace', (url) => {
    expect(accountWorkspaceLocation(url)?.clerkId).toBe('clrk_spec');
  });

  it('decodes the identities the URL escaped', () => {
    expect(accountWorkspaceLocation('/brokers/alpaca/clerks/clrk%20one/accounts/PA%2F9')).toEqual({
      broker: 'alpaca',
      clerkId: 'clrk one',
      accountId: 'PA/9',
      tab: 'overview',
    });
  });

  it.each([
    ['/brokers/alpaca', 'the account list'],
    ['/brokers/alpaca/bots', 'a broker-wide chooser'],
    ['/brokers/alpaca/clerks/clrk_spec', 'a clerk without an account'],
    ['/brokers/alpaca/clerks/clrk_spec/bots', 'a clerk-only surface'],
    ['/brokers/alpaca/clerks/clrk_spec/configuration', "the lane's configuration page"],
    [`${WORKSPACE}/bots/sid-1`, "a bot's own page"],
    [`${WORKSPACE}/unknown`, 'an unknown tab segment'],
    ['/edge/regimes', 'an unrelated route'],
  ])('reports %s (%s) as outside any workspace', (url) => {
    expect(accountWorkspaceLocation(url)).toBeNull();
  });
});

describe('accountWorkspaceTabRoute', () => {
  it.each([
    ['overview' as const, ['/brokers', 'alpaca', 'clerks', 'clrk_spec', 'accounts', 'PA9']],
    ['bots' as const, ['/brokers', 'alpaca', 'clerks', 'clrk_spec', 'accounts', 'PA9', 'bots']],
    ['gallery' as const, ['/brokers', 'alpaca', 'clerks', 'clrk_spec', 'accounts', 'PA9', 'gallery']],
    // Configuration stays lane-scoped: it carries no account segment until a
    // later slice renders it inside the workspace.
    ['configuration' as const, ['/brokers', 'alpaca', 'clerks', 'clrk_spec', 'configuration']],
  ])('builds the %s tab route', (tab, expected) => {
    expect(accountWorkspaceTabRoute(LOCATION, tab)).toEqual(expected);
  });

  it('round-trips every routed tab back through the resolver', () => {
    for (const tab of ['overview', 'bots', 'gallery'] as const) {
      const url = accountWorkspaceTabRoute(LOCATION, tab).join('/');
      expect(accountWorkspaceLocation(url)).toEqual({ ...LOCATION, tab });
    }
  });
});

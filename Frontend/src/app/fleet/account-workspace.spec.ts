import { describe, expect, it } from 'vitest';

import {
  accountWorkspaceBadgeRoute,
  accountWorkspaceBotRoute,
  accountWorkspaceDeployAgainRoute,
  accountWorkspaceEntryRoute,
  accountWorkspaceFixRoute,
  accountWorkspaceHomeRoute,
  accountWorkspaceLocation,
  accountWorkspaceTabRoute,
  accountWorkspaceTitle,
  type AccountWorkspaceLocation,
} from './account-workspace';

const LANE = '/brokers/alpaca/clerks/clrk_spec';
const WORKSPACE = `${LANE}/accounts/PA9`;

const LOCATION: AccountWorkspaceLocation = {
  broker: 'alpaca',
  clerkId: 'clrk_spec',
  accountId: 'PA9',
  tab: 'home',
  botSid: null,
};

/** A lane with no confirmed account: the same workspace, addressed without an
 * account segment (FR-092). */
const LANE_ONLY: AccountWorkspaceLocation = { ...LOCATION, accountId: null, tab: 'settings' };

const ACCOUNT = { broker: 'alpaca', clerkId: 'clrk_spec', accountId: 'PA9' };

describe('accountWorkspaceLocation', () => {
  it.each([
    [WORKSPACE, 'home'],
    [`${WORKSPACE}?view=wall`, 'home'],
    [`${WORKSPACE}/deploy`, 'deploy'],
    [`${WORKSPACE}/activity`, 'activity'],
  ] as const)('resolves the account-scoped %s to the %s tab', (url, tab) => {
    expect(accountWorkspaceLocation(url)).toEqual({ ...LOCATION, tab });
  });

  it.each([
    [`${LANE}/settings`, 'settings'],
    [`${LANE}/home`, 'home'],
  ] as const)('resolves the lane-scoped %s to the %s tab with no account', (url, tab) => {
    expect(accountWorkspaceLocation(url)).toEqual({ ...LANE_ONLY, tab });
  });

  it.each([
    [`${WORKSPACE}?lens=operator`, 'a query string'],
    [`${WORKSPACE}#positions`, 'a fragment'],
    [`${WORKSPACE}/`, 'a trailing slash'],
    [`${WORKSPACE}/deploy?from=sid-1`, 'a query string on a tab'],
  ])('resolves %s — %s never hides the workspace', (url) => {
    expect(accountWorkspaceLocation(url)?.clerkId).toBe('clrk_spec');
  });

  it('decodes the identities the URL escaped', () => {
    expect(accountWorkspaceLocation('/brokers/alpaca/clerks/clrk%20one/accounts/PA%2F9')).toEqual({
      broker: 'alpaca',
      clerkId: 'clrk one',
      accountId: 'PA/9',
      tab: 'home',
      botSid: null,
    });
  });

  it.each([
    ['/brokers/alpaca', 'the account list'],
    ['/brokers/alpaca/bots', 'a broker-wide chooser'],
    ['/brokers/alpaca/clerks/clrk_spec', 'a lane without a tab'],
    [`${WORKSPACE}/deploy/sid-1`, 'a stray segment under Deploy'],
    [`${WORKSPACE}/unknown`, 'an unknown tab segment'],
    ['/edge/regimes', 'an unrelated route'],
  ])('reports %s (%s) as outside any workspace', (url) => {
    expect(accountWorkspaceLocation(url)).toBeNull();
  });

  it("puts a bot's own page under Home, whatever its query says", () => {
    for (const url of [`${WORKSPACE}/bots/sid-1`, `${WORKSPACE}/bots/sid-1?from=gallery`]) {
      expect(accountWorkspaceLocation(url)).toEqual({ ...LOCATION, botSid: 'sid-1' });
    }
    expect(accountWorkspaceLocation(`${WORKSPACE}/bots/sid%2F1`)?.botSid).toBe('sid/1');
  });
});

describe('accountWorkspaceTabRoute', () => {
  it.each([
    ['home' as const, ['/brokers', 'alpaca', 'clerks', 'clrk_spec', 'accounts', 'PA9']],
    ['deploy' as const, ['/brokers', 'alpaca', 'clerks', 'clrk_spec', 'accounts', 'PA9', 'deploy']],
    ['activity' as const, ['/brokers', 'alpaca', 'clerks', 'clrk_spec', 'accounts', 'PA9', 'activity']],
    // Settings is lane-scoped wherever it is opened from (FR-092).
    ['settings' as const, ['/brokers', 'alpaca', 'clerks', 'clrk_spec', 'settings']],
  ])('builds the %s tab route for a bound account', (tab, expected) => {
    expect(accountWorkspaceTabRoute(LOCATION, tab)).toEqual(expected);
  });

  it.each([
    ['home' as const, ['/brokers', 'alpaca', 'clerks', 'clrk_spec', 'home']],
    ['settings' as const, ['/brokers', 'alpaca', 'clerks', 'clrk_spec', 'settings']],
  ])('addresses the %s tab of a lane with no account in place', (tab, expected) => {
    expect(accountWorkspaceTabRoute(LANE_ONLY, tab)).toEqual(expected);
  });

  it('has no Deploy to offer a lane with no account', () => {
    // Deploy binds a strategy to the account. Offering a substitute would be
    // the redirect-to-another-lane FR-096 forbids; the tab says so instead.
    expect(accountWorkspaceTabRoute(LANE_ONLY, 'deploy')).toBeNull();
  });

  it("has no Activity to offer a lane with no account", () => {
    // Activity is the account's own history; a lane without one has none.
    expect(accountWorkspaceTabRoute(LANE_ONLY, 'activity')).toBeNull();
  });

  it('round-trips every routed tab back through the resolver', () => {
    for (const tab of ['home', 'activity', 'deploy', 'settings'] as const) {
      const url = accountWorkspaceTabRoute(LOCATION, tab)?.join('/') ?? '';
      expect(accountWorkspaceLocation(url)).toEqual(
        tab === 'settings' ? { ...LANE_ONLY, tab } : { ...LOCATION, tab },
      );
    }
    for (const tab of ['home', 'settings'] as const) {
      const url = accountWorkspaceTabRoute(LANE_ONLY, tab)?.join('/') ?? '';
      expect(accountWorkspaceLocation(url)).toEqual({ ...LANE_ONLY, tab });
    }
  });

  it("agrees with Home's own route, which always resolves", () => {
    for (const address of [LOCATION, LANE_ONLY]) {
      expect(accountWorkspaceHomeRoute(address)).toEqual(accountWorkspaceTabRoute(address, 'home'));
    }
  });
});

describe('the links Home hands out', () => {
  it("opens a bot's page under Home, with nothing stamped on it", () => {
    expect(accountWorkspaceBotRoute(ACCOUNT, 'sid-1')).toEqual({
      commands: ['/brokers', 'alpaca', 'clerks', 'clrk_spec', 'accounts', 'PA9', 'bots', 'sid-1'],
      queryParams: {},
    });
  });

  it('starts Deploy again from an ended bot, on the Deploy page', () => {
    const link = accountWorkspaceDeployAgainRoute(ACCOUNT, 'sid-1');

    expect(link).toEqual({
      commands: ['/brokers', 'alpaca', 'clerks', 'clrk_spec', 'accounts', 'PA9', 'deploy'],
      queryParams: { from: 'sid-1' },
    });
    expect(accountWorkspaceLocation(`${link.commands.join('/')}?from=sid-1`)).toEqual({ ...LOCATION, tab: 'deploy' });
  });

  it.each([
    ['bot' as const, 'sid-1', ['/brokers', 'alpaca', 'clerks', 'clrk_spec', 'accounts', 'PA9', 'bots', 'sid-1']],
    ['activity' as const, null, ['/brokers', 'alpaca', 'clerks', 'clrk_spec', 'accounts', 'PA9', 'activity']],
    // Settings stays lane-scoped wherever it is opened from.
    ['settings' as const, null, ['/brokers', 'alpaca', 'clerks', 'clrk_spec', 'settings']],
  ])('links an attention line whose fix lives at %s', (destination, sid, commands) => {
    expect(accountWorkspaceFixRoute(ACCOUNT, destination, sid)?.commands).toEqual(commands);
  });

  it('links no bot fix that names no bot', () => {
    expect(accountWorkspaceFixRoute(ACCOUNT, 'bot', null)).toBeNull();
  });
});

describe('accountWorkspaceEntryRoute', () => {
  it('opens a confirmed account on its own Home', () => {
    expect(accountWorkspaceEntryRoute(LOCATION)).toEqual([
      '/brokers', 'alpaca', 'clerks', 'clrk_spec', 'accounts', 'PA9',
    ]);
  });

  it('opens a lane with no confirmed account on Settings, where binding it happens', () => {
    expect(accountWorkspaceEntryRoute({ ...LOCATION, accountId: null })).toEqual([
      '/brokers', 'alpaca', 'clerks', 'clrk_spec', 'settings',
    ]);
  });
});

describe('accountWorkspaceBadgeRoute', () => {
  const target = { broker: 'alpaca', clerkId: 'clrk_live', accountId: 'PA_LIVE' };

  it.each([
    ['home' as const, ['/brokers', 'alpaca', 'clerks', 'clrk_live', 'accounts', 'PA_LIVE']],
    ['activity' as const, ['/brokers', 'alpaca', 'clerks', 'clrk_live', 'accounts', 'PA_LIVE', 'activity']],
    ['deploy' as const, ['/brokers', 'alpaca', 'clerks', 'clrk_live', 'accounts', 'PA_LIVE', 'deploy']],
    ['settings' as const, ['/brokers', 'alpaca', 'clerks', 'clrk_live', 'settings']],
  ])('keeps the %s tab when a pill is clicked from inside a workspace', (tab, expected) => {
    expect(accountWorkspaceBadgeRoute({ ...LOCATION, tab }, target).commands).toEqual(expected);
  });

  it("lands on the chosen account's Home from a bot's page", () => {
    // The chosen account need not run this bot, so the bot's page itself is
    // never carried across (ADR 0064 Decision 4).
    const from: AccountWorkspaceLocation = { ...LOCATION, botSid: 'sid-1' };

    expect(accountWorkspaceBadgeRoute(from, target).commands).toEqual([
      '/brokers', 'alpaca', 'clerks', 'clrk_live', 'accounts', 'PA_LIVE',
    ]);
  });

  it.each([
    ['activity' as const, ['/brokers', 'alpaca', 'clerks', 'clrk_live', 'settings']],
    ['deploy' as const, ['/brokers', 'alpaca', 'clerks', 'clrk_live', 'settings']],
    ['home' as const, ['/brokers', 'alpaca', 'clerks', 'clrk_live', 'home']],
  ])('lands the %s tab on an unconfirmed account in place', (tab, expected) => {
    const unbound = { ...target, accountId: null };

    expect(accountWorkspaceBadgeRoute({ ...LOCATION, tab }, unbound).commands).toEqual(expected);
  });

  it('carries nothing that was open over the workspace, and no retired lens', () => {
    // An open Deploy form, a Wall view and a selected timeline are all
    // `?`-addressed workspace state, and `?lens=` is retired (PRD #2560).
    // The pill builds an empty query rather than merging, so none of them
    // can retarget at the other account.
    expect(accountWorkspaceBadgeRoute(LOCATION, target).queryParams).toEqual({});
  });

  it("lands on the chosen account's Home from outside any workspace", () => {
    expect(accountWorkspaceBadgeRoute(null, target)).toEqual({
      commands: ['/brokers', 'alpaca', 'clerks', 'clrk_live', 'accounts', 'PA_LIVE'],
      queryParams: {},
    });
  });

  it('lands on Settings from outside a workspace when the account is unconfirmed', () => {
    expect(
      accountWorkspaceBadgeRoute(null, { ...target, accountId: null }).commands,
    ).toEqual(['/brokers', 'alpaca', 'clerks', 'clrk_live', 'settings']);
  });

  it("opens another broker's account at its own front door, never under the broker being left", () => {
    const link = accountWorkspaceBadgeRoute(
      LOCATION,
      { broker: 'webull', clerkId: 'clrk_wb', accountId: 'WB1' },
    );

    expect(link.commands).toEqual(['/brokers', 'webull', 'clerks', 'clrk_wb', 'accounts', 'WB1']);
    expect(link.queryParams).toEqual({});
  });
});

describe('accountWorkspaceTitle', () => {
  it.each([
    ['home' as const, 'Home · Paper'],
    ['settings' as const, 'Settings · Paper'],
    ['activity' as const, 'Activity · Paper'],
    ['deploy' as const, 'Deploy a bot · Paper'],
  ])('titles the %s tab with the account name beside it', (tab, expected) => {
    expect(accountWorkspaceTitle(tab, 'Paper', null)).toBe(expected);
  });

  it("titles a bot's page by the bot, not by the tab it sits under", () => {
    expect(accountWorkspaceTitle('home', 'Paper', 'Deployment Validation')).toBe(
      'Deployment Validation · Paper',
    );
  });

  it('names only what is open when the account has no resolved name', () => {
    expect(accountWorkspaceTitle('settings', null, null)).toBe('Settings');
  });
});

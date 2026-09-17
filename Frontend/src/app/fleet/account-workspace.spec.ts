import { describe, expect, it } from 'vitest';

import {
  accountWorkspaceBotRoute,
  accountWorkspaceLocation,
  accountWorkspaceOriginTabRoute,
  accountWorkspaceSwitchRoute,
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
  tab: 'overview',
  botSid: null,
};

/** A lane with no confirmed account: the same workspace, addressed without an
 * account segment (FR-092). */
const LANE_ONLY: AccountWorkspaceLocation = { ...LOCATION, accountId: null, tab: 'configuration' };

describe('accountWorkspaceLocation', () => {
  it.each([
    [WORKSPACE, 'overview'],
    [`${WORKSPACE}/bots`, 'bots'],
    [`${WORKSPACE}/gallery`, 'gallery'],
  ] as const)('resolves the account-scoped %s to the %s tab', (url, tab) => {
    expect(accountWorkspaceLocation(url)).toEqual({ ...LOCATION, tab });
  });

  it.each([
    [`${LANE}/configuration`, 'configuration'],
    [`${LANE}/bots`, 'bots'],
    [`${LANE}/gallery`, 'gallery'],
  ] as const)('resolves the lane-scoped %s to the %s tab with no account', (url, tab) => {
    expect(accountWorkspaceLocation(url)).toEqual({ ...LANE_ONLY, tab });
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
      botSid: null,
    });
  });

  it.each([
    ['/brokers/alpaca', 'the account list'],
    ['/brokers/alpaca/bots', 'a broker-wide chooser'],
    ['/brokers/alpaca/clerks/clrk_spec', 'a lane without a tab'],
    [`${WORKSPACE}/gallery/sid-1`, 'a stray segment under Gallery'],
    [`${WORKSPACE}/unknown`, 'an unknown tab segment'],
    ['/edge/regimes', 'an unrelated route'],
  ])('reports %s (%s) as outside any workspace', (url) => {
    expect(accountWorkspaceLocation(url)).toBeNull();
  });

  describe("a bot's own page", () => {
    it.each([
      [`${WORKSPACE}/bots/sid-1?from=gallery`, 'gallery', 'the Gallery it was opened from'],
      [`${WORKSPACE}/bots/sid-1?from=bots`, 'bots', 'the roster it was opened from'],
      [`${WORKSPACE}/bots/sid-1`, 'bots', 'Bots when nothing stamped it'],
      [`${WORKSPACE}/bots/sid-1?from=elsewhere`, 'bots', 'Bots when the stamp is unknown'],
      [`${WORKSPACE}/bots/sid-1?lens=operator`, 'bots', 'Bots beside an unrelated parameter'],
    ] as const)('resolves %s to %s — %s', (url, tab, _reason) => {
      expect(accountWorkspaceLocation(url)).toEqual({ ...LOCATION, tab, botSid: 'sid-1' });
    });

    it('decodes the bot identity the URL escaped', () => {
      expect(accountWorkspaceLocation(`${WORKSPACE}/bots/sid%2F1`)?.botSid).toBe('sid/1');
    });
  });
});

describe('accountWorkspaceTabRoute', () => {
  it.each([
    ['overview' as const, ['/brokers', 'alpaca', 'clerks', 'clrk_spec', 'accounts', 'PA9']],
    ['bots' as const, ['/brokers', 'alpaca', 'clerks', 'clrk_spec', 'accounts', 'PA9', 'bots']],
    ['gallery' as const, ['/brokers', 'alpaca', 'clerks', 'clrk_spec', 'accounts', 'PA9', 'gallery']],
    // Configuration is lane-scoped wherever it is opened from (FR-092).
    ['configuration' as const, ['/brokers', 'alpaca', 'clerks', 'clrk_spec', 'configuration']],
  ])('builds the %s tab route for a bound account', (tab, expected) => {
    expect(accountWorkspaceTabRoute(LOCATION, tab)).toEqual(expected);
  });

  it.each([
    ['bots' as const, ['/brokers', 'alpaca', 'clerks', 'clrk_spec', 'bots']],
    ['gallery' as const, ['/brokers', 'alpaca', 'clerks', 'clrk_spec', 'gallery']],
    ['configuration' as const, ['/brokers', 'alpaca', 'clerks', 'clrk_spec', 'configuration']],
  ])('addresses the %s tab of a lane with no account in place', (tab, expected) => {
    expect(accountWorkspaceTabRoute(LANE_ONLY, tab)).toEqual(expected);
  });

  it('has no Overview to offer a lane with no account', () => {
    // Overview is the account's own page. Offering a substitute here would be
    // the redirect-to-another-lane FR-096 forbids; the tab says so instead.
    expect(accountWorkspaceTabRoute(LANE_ONLY, 'overview')).toBeNull();
  });

  it('round-trips every routed tab back through the resolver', () => {
    for (const tab of ['overview', 'bots', 'gallery'] as const) {
      const url = accountWorkspaceTabRoute(LOCATION, tab)?.join('/') ?? '';
      expect(accountWorkspaceLocation(url)).toEqual({ ...LOCATION, tab });
    }
    for (const tab of ['bots', 'gallery', 'configuration'] as const) {
      const url = accountWorkspaceTabRoute(LANE_ONLY, tab)?.join('/') ?? '';
      expect(accountWorkspaceLocation(url)).toEqual({ ...LANE_ONLY, tab });
    }
  });
});

describe('accountWorkspaceOriginTabRoute', () => {
  it.each([
    ['a bound account', LOCATION],
    ['a lane with no account', LANE_ONLY],
  ] as const)('resolves both origin tabs of %s without a null to guard', (_what, address) => {
    // The narrowed sibling exists so a bot's page, which can only ever have
    // come from Bots or Gallery, does not carry Overview's `null` into its
    // template. It must never disagree with the general route.
    for (const origin of ['bots', 'gallery'] as const) {
      expect(accountWorkspaceOriginTabRoute(address, origin)).toEqual(
        accountWorkspaceTabRoute(address, origin),
      );
    }
  });
});

describe('accountWorkspaceBotRoute', () => {
  const account = { broker: 'alpaca', clerkId: 'clrk_spec', accountId: 'PA9' };

  it.each([['bots'], ['gallery']] as const)('stamps a bot link opened from %s', (origin) => {
    const link = accountWorkspaceBotRoute(account, 'sid-1', origin);

    expect(link.commands).toEqual([
      '/brokers', 'alpaca', 'clerks', 'clrk_spec', 'accounts', 'PA9', 'bots', 'sid-1',
    ]);
    expect(link.queryParams).toEqual({ from: origin });
  });

  it('round-trips the stamp back into the tab the page belongs to', () => {
    for (const origin of ['bots', 'gallery'] as const) {
      const link = accountWorkspaceBotRoute(account, 'sid-1', origin);
      const url = `${link.commands.join('/')}?from=${link.queryParams['from']}`;

      expect(accountWorkspaceLocation(url)).toEqual({ ...LOCATION, tab: origin, botSid: 'sid-1' });
    }
  });
});

describe('accountWorkspaceSwitchRoute', () => {
  const target = { clerkId: 'clrk_live', accountId: 'PA_LIVE' };

  it.each([
    ['overview' as const, ['/brokers', 'alpaca', 'clerks', 'clrk_live', 'accounts', 'PA_LIVE']],
    ['bots' as const, ['/brokers', 'alpaca', 'clerks', 'clrk_live', 'accounts', 'PA_LIVE', 'bots']],
    ['gallery' as const, ['/brokers', 'alpaca', 'clerks', 'clrk_live', 'accounts', 'PA_LIVE', 'gallery']],
    ['configuration' as const, ['/brokers', 'alpaca', 'clerks', 'clrk_live', 'configuration']],
  ])('lands on the same %s tab of the chosen account', (tab, expected) => {
    expect(accountWorkspaceSwitchRoute({ ...LOCATION, tab }, target, null).commands).toEqual(expected);
  });

  it.each([['bots'], ['gallery']] as const)(
    "lands on the chosen account's Bots from a bot's page opened from %s",
    (origin) => {
      // The chosen account need not run this bot, so the bot's page itself is
      // never carried across (ADR 0064 Decision 4).
      const from: AccountWorkspaceLocation = { ...LOCATION, tab: origin, botSid: 'sid-1' };

      expect(accountWorkspaceSwitchRoute(from, target, null).commands).toEqual([
        '/brokers', 'alpaca', 'clerks', 'clrk_live', 'accounts', 'PA_LIVE', 'bots',
      ]);
    },
  );

  it.each([
    ['overview' as const, ['/brokers', 'alpaca', 'clerks', 'clrk_live', 'configuration']],
    ['bots' as const, ['/brokers', 'alpaca', 'clerks', 'clrk_live', 'bots']],
  ])('lands the %s tab on an unconfirmed account in place', (tab, expected) => {
    const unbound = { clerkId: 'clrk_live', accountId: null };

    expect(accountWorkspaceSwitchRoute({ ...LOCATION, tab }, unbound, null).commands).toEqual(expected);
  });

  it('carries the lens perspective across, and nothing else', () => {
    expect(accountWorkspaceSwitchRoute(LOCATION, target, 'operator').queryParams).toEqual({
      lens: 'operator',
    });
    // An open Deploy drawer, a selected custody timeline and a bot's origin
    // stamp are all `?`-addressed workspace state. The switch builds its query
    // rather than merging, so none of them can retarget at the other account.
    expect(accountWorkspaceSwitchRoute(LOCATION, target, null).queryParams).toEqual({});
  });
});

describe('accountWorkspaceTitle', () => {
  it.each([
    ['overview' as const, 'Overview · Paper'],
    ['bots' as const, 'Bots · Paper'],
    ['gallery' as const, 'Gallery · Paper'],
    ['configuration' as const, 'Configuration · Paper'],
  ])('titles the %s tab with the account name beside it', (tab, expected) => {
    expect(accountWorkspaceTitle(tab, 'Paper', null)).toBe(expected);
  });

  it("titles a bot's page by the bot, not by the tab it sits under", () => {
    expect(accountWorkspaceTitle('gallery', 'Paper', 'Deployment Validation')).toBe(
      'Deployment Validation · Paper',
    );
  });

  it('names only what is open when the account has no resolved name', () => {
    expect(accountWorkspaceTitle('configuration', null, null)).toBe('Configuration');
  });
});

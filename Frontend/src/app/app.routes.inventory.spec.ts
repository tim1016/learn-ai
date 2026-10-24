import { Location } from '@angular/common';
import { TestBed } from '@angular/core/testing';
import {
  NavigationCancel,
  NavigationEnd,
  NavigationError,
  Router,
  type Route,
  type Routes,
} from '@angular/router';
import { firstValueFrom } from 'rxjs';
import { filter } from 'rxjs/operators';
import { beforeAll, describe, expect, it } from 'vitest';

import { appConfig } from './app.config';
import { routes } from './app.routes';
import { TEST_ACCOUNT_ID, TEST_CLERK_ID, provideFleetDirectory } from './fleet/fleet-directory-testing';
import { activeMenuNodeFor, APP_MENU } from './shell/app-menu';
import { routeTreeHasData } from './shell/current-url.service';
import { HELP_DOCUMENTS } from './shared/help/help-documents';

/**
 * T01 (PRD #3490): every route the app declares, direct-loaded with a query
 * and a fragment, reaches the page its R32 row names — in one navigation, with
 * nothing lost on the way — and that page lights up the right menu group.
 *
 * The routes are read from the real route table (both lazy families
 * included), so a route added without a row here fails the first test, and a
 * row for a route that is gone fails the second. The 137 declarations recorded
 * on #3491 are these addresses: a shell and its `''` child share one.
 */

type GroupTitle = 'Markets' | 'Research' | 'Signal strategies' | 'Options setups' | 'Accounts' | 'Operations';

interface Row {
  /** The menu group the page ends in. `null`: it has none (the entry, a document, a lost address). */
  readonly group: GroupTitle | null;
  /** Where a redirect ends, as a URL without the probe query. Absent: the route is its own destination. */
  readonly lands?: string;
  /** The route ends in the not-found page. */
  readonly notFound?: true;
}

const MARKETS = 'Markets';
const RESEARCH = 'Research';
const SIGNAL = 'Signal strategies';
const OPTIONS = 'Options setups';
const ACCOUNTS = 'Accounts';
const OPERATIONS = 'Operations';

const ACCOUNT_LIST = '/brokers/alpaca';
const LANE = '/brokers/alpaca/clerks/clrk_spec';
const WORKSPACE = `${LANE}/accounts/PA9`;
const DOCUMENT: Row = { group: null };
const RETIRED_BROKER_ALIAS: Row = { group: ACCOUNTS, lands: ACCOUNT_LIST };

const ROWS: Readonly<Record<string, Row>> = {
  // ── Entry ────────────────────────────────────────────────────────────────
  '': { group: null },

  // ── Markets ──────────────────────────────────────────────────────────────
  markets: { group: MARKETS },
  'markets/:symbol': { group: MARKETS, lands: '/markets/SPY/chart' },
  'markets/:symbol/chart': { group: MARKETS },
  'markets/:symbol/chart/news': { group: MARKETS },
  // The chart moved under the instrument; with no instrument named, an old address leads to the list.
  'data-lab': { group: MARKETS, lands: '/markets' },
  'data-lab/explore': { group: MARKETS, lands: '/markets' },
  'data-lab/export': { group: MARKETS },
  'data-lab/returns': { group: MARKETS },
  'options-lab': { group: MARKETS, lands: '/options-lab/chain' },
  'options-lab/chain': { group: MARKETS },
  'options-lab/strategy-finder': { group: MARKETS },
  'broker/options-chain': { group: MARKETS },
  'broker/options-surface': { group: MARKETS },
  'pricing-lab': { group: MARKETS },
  'research-lab/market/news': { group: MARKETS, lands: '/markets' },

  // ── Research ─────────────────────────────────────────────────────────────
  'research-lab': { group: RESEARCH, lands: '/research-lab/features/validate' },
  'research-lab/features/validate': { group: RESEARCH },
  'research-lab/features/cross-sectional': { group: RESEARCH },
  'research-lab/features/experiments': { group: RESEARCH },
  'research-lab/signals/engine': { group: RESEARCH },
  'research-lab/signals/history': { group: RESEARCH },
  'research-lab/backtests/reliability': { group: RESEARCH },
  'research-lab/backtests/strategy-runs': { group: RESEARCH },
  'research-lab/strategy-runs/:run_id': { group: RESEARCH },
  'research-lab/walk-forward/:wf_id': { group: RESEARCH },
  'research-lab/monte-carlo/:mc_id': { group: RESEARCH },
  'research-lab/baselines/:baseline_id': { group: RESEARCH },
  'research-lab/signal-report/:id': { group: RESEARCH },
  'research-lab/build/validate': { group: RESEARCH, lands: '/research-lab/features/validate' },
  'research-lab/build/indicator-reliability': { group: RESEARCH, lands: '/research-lab/backtests/reliability' },
  'research-lab/build/signal-engine': { group: RESEARCH, lands: '/research-lab/signals/engine' },
  'research-lab/inspect/cross-sectional': { group: RESEARCH, lands: '/research-lab/features/cross-sectional' },
  'research-lab/inspect/strategy-runs': { group: RESEARCH, lands: '/research-lab/backtests/strategy-runs' },
  'research-lab/inspect/experiments': { group: RESEARCH, lands: '/research-lab/features/experiments' },
  'research-lab/inspect/signal-history': { group: RESEARCH, lands: '/research-lab/signals/history' },
  'research-lab/feature-runner': { group: RESEARCH, lands: '/research-lab/features/validate' },
  'research-lab/indicator-reliability': { group: RESEARCH, lands: '/research-lab/backtests/reliability' },
  'research-lab/signal-engine': { group: RESEARCH, lands: '/research-lab/signals/engine' },
  'research-lab/cross-sectional': { group: RESEARCH, lands: '/research-lab/features/cross-sectional' },
  'research-lab/experiment-history': { group: RESEARCH, lands: '/research-lab/features/experiments' },
  edge: { group: RESEARCH },
  'edge/realized-vs-iv': { group: RESEARCH },
  'edge/cross-asset': { group: RESEARCH },
  'edge/regimes': { group: RESEARCH },
  'spec-strategy': { group: RESEARCH },

  // ── Signal strategies ────────────────────────────────────────────────────
  'strategy-lab': { group: SIGNAL },
  'strategy-lab/runs/:id': { group: SIGNAL, lands: '/strategy-lab?run=7' },
  'engine/runs/:id': { group: SIGNAL, lands: '/strategy-lab?run=7' },
  engine: { group: SIGNAL, lands: '/strategy-lab' },
  'lean-engine': { group: SIGNAL, lands: '/strategy-lab' },
  'lean-lab': { group: SIGNAL, lands: '/strategy-lab' },
  'grid-search': { group: SIGNAL },
  'walk-forward': { group: SIGNAL },
  'golden-search': { group: SIGNAL },
  'golden-search/:studyId': { group: SIGNAL },
  'strategy-validation': { group: SIGNAL },
  'research-lab/backtests/recency-chart': { group: SIGNAL },

  // ── Options setups ───────────────────────────────────────────────────────
  'options-backtest': { group: OPTIONS },
  'options-golden-search': { group: OPTIONS },
  'options-golden-search/:studyId': { group: OPTIONS },
  'options-evidence/:kind/:subjectId': { group: OPTIONS },
  // The analytical builder is an Options Lab tab, filed under Options setups (R32).
  'options-lab/strategy-builder': { group: OPTIONS },

  // ── Operations ───────────────────────────────────────────────────────────
  'data-lake': { group: OPERATIONS },
  'golden-fixtures': { group: OPERATIONS },
  'data-lab/validate': { group: OPERATIONS },
  'data-quality': { group: OPERATIONS, lands: '/data-lab/validate' },
  'research-lab/inspect/data-divergence': { group: OPERATIONS },
  'research-lab/inspect/pre-flight': { group: OPERATIONS },
  'research-lab/data-divergence': { group: OPERATIONS, lands: '/research-lab/inspect/data-divergence' },
  'research-lab/strategy-preflight': { group: OPERATIONS, lands: '/research-lab/inspect/pre-flight' },

  // ── Documents: the help index is their home, so they have no group ───────
  'strategy-docs': DOCUMENT,
  'data-lab-docs': DOCUMENT,
  'indicator-docs': { group: null, lands: '/data-lab-docs' },
  'data-quality-docs': DOCUMENT,
  'strategy-lab/docs': DOCUMENT,
  'engine/docs': { group: null, lands: '/strategy-lab/docs' },
  'engine-docs': { group: null, lands: '/strategy-lab/docs' },
  'docs/options-backtest-manual': DOCUMENT,
  'docs/broker-manual': DOCUMENT,
  'docs/bot-business-manual': DOCUMENT,
  'docs/architecture-manual': DOCUMENT,
  'docs/indicator-reliability-methodology': DOCUMENT,
  'docs/signal-engine-methodology': DOCUMENT,
  'docs/golden-search-manual': DOCUMENT,
  'docs/golden-search-guide': DOCUMENT,
  'legal/notices': DOCUMENT,

  // ── Accounts: existing semantics, unchanged ──────────────────────────────
  'brokers/alpaca': { group: ACCOUNTS },
  'brokers/alpaca/settings': RETIRED_BROKER_ALIAS,
  'brokers/alpaca/configuration': RETIRED_BROKER_ALIAS,
  'brokers/alpaca/bots': RETIRED_BROKER_ALIAS,
  'brokers/alpaca/gallery': RETIRED_BROKER_ALIAS,
  'brokers/alpaca/deploy': { group: ACCOUNTS },
  'brokers/alpaca/accounts/:accountId/deploy': { group: ACCOUNTS },
  'brokers/alpaca/clerks/:clerkId': { group: ACCOUNTS, lands: `${LANE}/settings` },
  'brokers/alpaca/clerks/:clerkId/settings': { group: ACCOUNTS },
  'brokers/alpaca/clerks/:clerkId/history': { group: ACCOUNTS },
  'brokers/alpaca/clerks/:clerkId/configuration': { group: ACCOUNTS, lands: `${LANE}/settings` },
  'brokers/alpaca/clerks/:clerkId/home': { group: ACCOUNTS },
  'brokers/alpaca/clerks/:clerkId/bots': { group: ACCOUNTS, lands: `${LANE}/home` },
  'brokers/alpaca/clerks/:clerkId/gallery': { group: ACCOUNTS, lands: `${LANE}/home?view=wall` },
  'brokers/alpaca/clerks/:clerkId/accounts/:accountId': { group: ACCOUNTS },
  'brokers/alpaca/clerks/:clerkId/accounts/:accountId/bots/:sid': { group: ACCOUNTS },
  'brokers/alpaca/clerks/:clerkId/accounts/:accountId/bots': { group: ACCOUNTS, lands: WORKSPACE },
  'brokers/alpaca/clerks/:clerkId/accounts/:accountId/gallery': { group: ACCOUNTS, lands: `${WORKSPACE}?view=wall` },
  'brokers/alpaca/clerks/:clerkId/accounts/:accountId/activity': { group: ACCOUNTS },
  'brokers/alpaca/clerks/:clerkId/accounts/:accountId/deploy': { group: ACCOUNTS },
  broker: RETIRED_BROKER_ALIAS,
  'broker/accounts': RETIRED_BROKER_ALIAS,
  'broker/accounts/:accountId': RETIRED_BROKER_ALIAS,
  'broker/account-monitor': RETIRED_BROKER_ALIAS,
  'broker/reconciliation': RETIRED_BROKER_ALIAS,
  'broker/orders': RETIRED_BROKER_ALIAS,
  'broker/session-mirror': RETIRED_BROKER_ALIAS,
  'broker/paper-run': RETIRED_BROKER_ALIAS,
  'broker/instances': RETIRED_BROKER_ALIAS,
  'broker/instances/:id': RETIRED_BROKER_ALIAS,
  'broker/bots': RETIRED_BROKER_ALIAS,
  'broker/bots/:id': RETIRED_BROKER_ALIAS,
  'broker/offline-replay': RETIRED_BROKER_ALIAS,
  'broker/deploy': RETIRED_BROKER_ALIAS,
  'brokers/:broker/accounts/:accountId/bots/:sid': { group: ACCOUNTS },
  'brokers/:broker/accounts/:accountId/bots': { group: ACCOUNTS },
  'brokers/:broker/accounts/:accountId/gallery': { group: ACCOUNTS },
  'brokers/:broker/clerks/:clerkId/settings': { group: ACCOUNTS },
  'brokers/:broker/clerks/:clerkId/configuration': { group: ACCOUNTS, lands: '/brokers/ibkr/clerks/clrk_spec/settings' },
  'brokers/:broker/clerks/:clerkId/accounts/:accountId/bots/:sid': { group: ACCOUNTS },
  'brokers/:broker/clerks/:clerkId/accounts/:accountId/bots': { group: ACCOUNTS },
  'brokers/:broker/clerks/:clerkId/accounts/:accountId/gallery': { group: ACCOUNTS },
  'brokers/:broker/clerks/:clerkId/accounts/:accountId': { group: ACCOUNTS },
  'brokers/:broker/clerks/:clerkId': { group: ACCOUNTS },
  'brokers/:broker/bots': { group: ACCOUNTS },
  'brokers/:broker/gallery': { group: ACCOUNTS },

  // ── Anything else: fails in place ────────────────────────────────────────
  '**': { group: null, notFound: true },
};

/** Stand-ins for a route's params. Anything unlisted is a plain id. */
const SAMPLE_PARAMS: Readonly<Record<string, string>> = {
  broker: 'ibkr',
  clerkId: 'clrk_spec',
  accountId: 'PA9',
  sid: 'sid-1',
  kind: 'run',
  symbol: 'SPY',
};

/** The query and fragment every direct load carries, to prove neither is lost. */
const PROBE = { query: { probe: '1' }, fragment: 'probe' } as const;

/** Every address the route table declares, shells and `''` children sharing their parent's. */
async function declaredPaths(declared: Routes, prefix = ''): Promise<string[]> {
  const found: string[] = [];
  for (const route of declared) {
    const path = [prefix, route.path ?? ''].filter((part) => part !== '').join('/');
    found.push(path);
    found.push(...(await declaredPaths(await childrenOf(route), path)));
  }
  return found;
}

async function childrenOf(route: Route): Promise<Routes> {
  if (route.children !== undefined) return route.children;
  if (typeof route.loadChildren === 'function') return (await route.loadChildren()) as Routes;
  return [];
}

function addressFor(path: string): string {
  if (path === '**') return '/no-such-page';
  return `/${path.replace(/:(\w+)/g, (_, name: string) => SAMPLE_PARAMS[name] ?? '7')}`;
}

describe('the route inventory (T01)', () => {
  let declared: readonly string[];

  beforeAll(async () => {
    declared = [...new Set(await declaredPaths(routes))];
  });

  it('has a row for every route the app declares, and no row for a route that is gone', () => {
    expect(declared.filter((path) => !(path in ROWS)).sort(), 'routes without a row').toEqual([]);
    expect(Object.keys(ROWS).filter((path) => !declared.includes(path)).sort(), 'rows without a route').toEqual([]);
  });

  it('gives every menu entry and owned page a route the app declares', () => {
    const owned = APP_MENU.flatMap((group) => [...group.items, ...(group.pages ?? [])].map((item) => item.route));
    const unrouted = owned.filter(
      (route) => !declared.some((path) => `/${path}` === route || `/${path}`.startsWith(`${route}/`)),
    );

    expect(unrouted).toEqual([]);
  });

  it('keeps every served document out of the groups, whichever page it explains', () => {
    for (const document of HELP_DOCUMENTS) {
      expect.soft(activeMenuNodeFor(document.route), `${document.title} has no group`).toBeNull();
    }
  });

  describe('direct load', () => {
    const fleet = provideFleetDirectory();

    function configure() {
      TestBed.resetTestingModule();
      TestBed.configureTestingModule({
        providers: [...appConfig.providers, { provide: fleet.provide, useValue: fleet.useValue }],
      });
      return TestBed.inject(Router);
    }

    /** Navigates once and reports everything the router did to get there. */
    async function load(url: string) {
      const router = configure();
      const ended: string[] = [];
      const cancelled: unknown[] = [];
      const failed: unknown[] = [];
      router.events.subscribe((event) => {
        if (event instanceof NavigationEnd) ended.push(event.urlAfterRedirects);
        if (event instanceof NavigationCancel) cancelled.push(event.reason);
        if (event instanceof NavigationError) failed.push(event.error);
      });
      await router.navigateByUrl(url);
      return { router, ended, cancelled, failed };
    }

    function expectedTree(router: Router, lands: string) {
      const tree = router.parseUrl(lands);
      return {
        path: lands.split(/[?#]/)[0],
        query: { ...tree.queryParams, ...PROBE.query },
        fragment: PROBE.fragment,
      };
    }

    it.each(Object.entries(ROWS))(
      '%s ends where its row says, in one navigation, with its query and fragment, in the right group',
      async (path, row) => {
        const address = addressFor(path);
        const probeUrl = `${address}?${new URLSearchParams(PROBE.query).toString()}#${PROBE.fragment}`;
        const { router, ended, cancelled, failed } = await load(path === '' ? '/' : probeUrl);

        expect.soft(failed, 'no router error, loop or unmatched address').toEqual([]);
        expect.soft(ended, 'ends one navigation').toHaveLength(1);
        expect.soft(cancelled, 'is not redirected a second time by a guard').toEqual([]);

        const landed = router.parseUrl(router.url);
        if (path === '') {
          expect(router.url).toBe('/');
        } else {
          const want = expectedTree(router, row.lands ?? address);
          expect.soft(router.url.split(/[?#]/)[0], 'lands on the page the row names').toBe(want.path);
          expect.soft(landed.queryParams, 'keeps the query').toEqual(want.query);
          expect.soft(landed.fragment, 'keeps the fragment').toBe(want.fragment);
        }

        expect.soft(routeTreeHasData(router.routerState.snapshot.root, 'notFound'), 'fails in place only where it should').toBe(row.notFound === true);

        const node = row.notFound === true ? null : activeMenuNodeFor(router.url);
        expect.soft(node?.group.title ?? null, 'highlights the owning group').toBe(row.group);
      },
    );

    it.each([
      ['an account workspace', `${WORKSPACE}/activity?lens=operator`, `${WORKSPACE}/activity`],
      ['the lane address', `${LANE}?lens=operator&profileId=p1`, `${LANE}/settings?profileId=p1`],
      ['the retired Configuration tab', `${LANE}/configuration?lens=trader&profileId=p1&revision=2`, `${LANE}/settings?profileId=p1&revision=2`],
      ['a retired Gallery tab', `${WORKSPACE}/gallery?lens=operator`, `${WORKSPACE}?view=wall`],
      ['a retired Bots tab', `${LANE}/bots?lens=operator`, `${LANE}/home`],
      ['a retired broker-wide chooser', '/brokers/alpaca/bots?lens=operator&deploy=1&strategy=momentum', `${ACCOUNT_LIST}?deploy=1&strategy=momentum`],
    ])('drops a retired ?lens= from %s without a second move', async (_name, url, landsOn) => {
      const { router, ended, cancelled } = await load(url);

      expect.soft(router.url).toBe(landsOn);
      expect.soft(ended).toHaveLength(1);
      // The workspace's lens guard moves a plain tab URL once, straight to the final address.
      if (url.includes('/activity?')) expect.soft(cancelled).toHaveLength(1);
      else expect.soft(cancelled, 'redirects within the one navigation').toEqual([]);
    });

    it.each([
      ['bots', '/brokers/alpaca?surface=bots&deploy=1&strategy=momentum&golden_qualification=abc#go'],
      ['gallery', '/brokers/alpaca?surface=gallery&deploy=1&strategy=momentum&golden_qualification=abc#go'],
    ])('keeps a deploy intent through the retired ?surface=%s hint, going straight to the account list', async (_name, url) => {
      const { router, ended } = await load(url);

      expect(router.url).toBe(`${ACCOUNT_LIST}?deploy=1&strategy=momentum&golden_qualification=abc#go`);
      expect(ended).toEqual([router.url]);
    });

    it.each([
      ['a lane', `${LANE}/bots?view=wall&probe=1`, `${LANE}/home?probe=1`],
      ['an account', `${WORKSPACE}/bots?view=wall&probe=1`, `${WORKSPACE}?probe=1`],
    ])('lands a Bots bookmark of %s on Home\'s list, though it carries ?view=wall', async (_name, url, landsOn) => {
      const { router } = await load(url);

      expect(router.url).toBe(landsOn);
    });

    // The rows above probe these three unscoped addresses with the `ibkr`
    // stand-in, which fails in place. With a ready Alpaca lane the guard
    // resolves the account and moves once, to its Home.
    it.each([
      ['Bots', '', ''],
      ['Gallery', '/gallery', '&view=wall'],
    ])('lands the unscoped Alpaca %s address on the account Home with its query and fragment', async (_name, suffix, view) => {
      const home = `/brokers/alpaca/clerks/${TEST_CLERK_ID}/accounts/${TEST_ACCOUNT_ID}`;
      const { router, ended, failed } = await load(
        `/brokers/alpaca/accounts/${TEST_ACCOUNT_ID}${suffix === '' ? '/bots' : suffix}?probe=1&lens=operator#probe`,
      );

      expect.soft(failed).toEqual([]);
      expect.soft(ended).toHaveLength(1);
      expect(router.url).toBe(`${home}?probe=1${view}#probe`);
    });

    it('keeps the page and its group through Back and Forward', async () => {
      const router = configure();
      // Back and Forward arrive as the browser's popstate, which the router only
      // hears once its initial navigation has set the listener up.
      router.initialNavigation();
      const location = TestBed.inject(Location);
      const nextEnd = () => firstValueFrom(router.events.pipe(filter((event) => event instanceof NavigationEnd)));

      await router.navigateByUrl('/options-evidence/run/7?view=lineage');
      await router.navigateByUrl('/markets/SPY/chart?from=2026-01-02&to=2026-02-27');

      const back = nextEnd();
      location.back();
      await back;
      expect(router.url).toBe('/options-evidence/run/7?view=lineage');
      expect(activeMenuNodeFor(router.url)?.group.title).toBe('Options setups');

      const forward = nextEnd();
      location.forward();
      await forward;
      expect(router.url).toBe('/markets/SPY/chart?from=2026-01-02&to=2026-02-27');
      expect(activeMenuNodeFor(router.url)?.group.title).toBe('Markets');
    });

    it('keeps the Returns study on a legacy-scope link instead of moving to Explore', async () => {
      const { router } = await load('/data-lab/returns?ticker=SPY&from=2026-01-02');

      expect(router.url).toBe('/data-lab/returns?ticker=SPY&from=2026-01-02');
    });

    it.each([
      ['/data-lab?mode=build&ticker=SPY#option-archive-SPY', '/data-lab/export?ticker=SPY#option-archive-SPY'],
      ['/data-lab?mode=validate', '/data-lab/validate'],
      ['/data-lab?mode=bogus&ticker=SPY', '/markets/SPY/chart?mode=bogus'],
      ['/data-lab?mode=explore&ticker=spy&from=2026-01-02&to=2026-02-27&trading-session=extended#top', '/markets/SPY/chart?from=2026-01-02&to=2026-02-27&session=extended#top'],
      ['/data-lab/explore?ticker=QQQ&from=2026-01-02&to=2026-02-27', '/markets/QQQ/chart?from=2026-01-02&to=2026-02-27'],
      ['/data-lab/explore?ticker=QQQ&mode=validate', '/data-lab/validate?ticker=QQQ'],
      // The old news page named no instrument in its address; a ticker in the query is the instrument, and with none it leads to the list.
      ['/research-lab/market/news?ticker=SPY&from=2026-01-02&to=2026-02-27#top', '/markets/SPY/chart/news?from=2026-01-02&to=2026-02-27#top'],
      ['/research-lab/market/news', '/markets'],
      // A symbol this app cannot hold is carried to the instrument's address, where it fails in place; it is never swapped for another.
      ['/data-lab/explore?ticker=%24%24%24', '/markets/$$$/chart'],
      ['/data-quality?ticker=SPY&from=2026-01-02&foo=1', '/data-lab/validate?ticker=SPY&from=2026-01-02&foo=1'],
      ['/data-quality?mode=build&ticker=SPY', '/data-lab/export?ticker=SPY'],
    ])('lands the legacy Data Lab address %s on %s in one navigation', async (url, landsOn) => {
      const { router, ended, cancelled } = await load(url);

      expect(router.url).toBe(landsOn);
      expect(ended).toHaveLength(1);
      expect(cancelled).toEqual([]);
    });

    it('keeps everything else a persisted-run bookmark carried, and lets the path decide the run', async () => {
      const { router } = await load('/strategy-lab/runs/204?tab=history&run=9#trades');

      expect(router.url).toBe('/strategy-lab?tab=history&run=204#trades');
    });

    it('keeps a review request on the non-Alpaca Configuration alias', async () => {
      const { router } = await load('/brokers/ibkr/clerks/clrk_other/configuration?profileId=p1&revision=2');

      expect(router.url).toBe('/brokers/ibkr/clerks/clrk_other/settings?profileId=p1&revision=2');
    });
  });
});

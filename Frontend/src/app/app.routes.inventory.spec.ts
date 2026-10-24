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
import { PICK_TAB_KEY, PICK_VIEW_KEY, keysForPick, openableTab } from './markets/instrument-address';
import { optionsViewCommands } from './markets/options/options-address';
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
  'markets/:symbol/returns': { group: MARKETS },
  'markets/:symbol/data': { group: MARKETS },
  'markets/:symbol/data/build': { group: MARKETS },
  'markets/:symbol/options': { group: MARKETS, lands: '/markets/SPY/options/chain' },
  'markets/:symbol/options/chain': { group: MARKETS },
  'markets/:symbol/options/surface': { group: MARKETS },
  'markets/:symbol/options/pricing': { group: MARKETS },
  // The chart, the Returns study and the dataset builder moved under the instrument; with no instrument named, an old address leads to the list.
  'data-lab': { group: MARKETS, lands: '/markets' },
  'data-lab/explore': { group: MARKETS, lands: '/markets' },
  'data-lab/export': { group: MARKETS, lands: '/markets' },
  'data-lab/returns': { group: MARKETS, lands: '/markets' },
  // The chain moved into the instrument's Options tab; with no instrument named, an old address asks for one.
  'options-lab': { group: MARKETS, lands: '/markets?then=options' },
  'options-lab/chain': { group: MARKETS, lands: '/markets?then=options' },
  'options-lab/strategy-finder': { group: MARKETS },
  // This page was the IBKR feed, so with no instrument named the list is asked to open the chain on Now · Live IBKR.
  'broker/options-chain': { group: MARKETS, lands: '/markets?source=live&then=options' },
  // Surface and Pricing moved under the instrument; with no instrument named, an old address leads to the list.
  'broker/options-surface': { group: MARKETS, lands: '/markets?then=options&view=surface' },
  'pricing-lab': { group: MARKETS, lands: '/markets?then=options&view=pricing' },
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
  'data-lake': { group: OPERATIONS, lands: '/operations/lake' },
  operations: { group: OPERATIONS, lands: '/operations/lake' },
  'operations/lake': { group: OPERATIONS },
  'operations/option-archive': { group: OPERATIONS },
  'operations/backups': { group: OPERATIONS },
  'operations/math-checks': { group: OPERATIONS },
  'operations/data-checks': { group: OPERATIONS, lands: '/operations/data-checks/tradingview' },
  'operations/data-checks/tradingview': { group: OPERATIONS },
  'operations/data-checks/divergence': { group: OPERATIONS },
  'operations/data-checks/pre-flight': { group: OPERATIONS },
  'golden-fixtures': { group: OPERATIONS, lands: '/operations/math-checks' },
  'data-lab/validate': { group: OPERATIONS, lands: '/operations/data-checks/tradingview' },
  'data-quality': { group: OPERATIONS, lands: '/operations/data-checks/tradingview' },
  'research-lab/inspect/data-divergence': { group: OPERATIONS, lands: '/operations/data-checks/divergence' },
  'research-lab/inspect/pre-flight': { group: OPERATIONS, lands: '/operations/data-checks/pre-flight' },
  'research-lab/data-divergence': { group: OPERATIONS, lands: '/operations/data-checks/divergence' },
  'research-lab/strategy-preflight': { group: OPERATIONS, lands: '/operations/data-checks/pre-flight' },

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
      ['the Surface 3D address', '/broker/options-surface?ticker=spy&expiry=1790000000000#top', '/markets/SPY/options/surface?expiry=1790000000000#top'],
      ['the Pricing Lab address', '/pricing-lab?symbol=qqq&expiry=1790000000000#top', '/markets/QQQ/options/pricing?expiry=1790000000000#top'],
      ['the Pricing Lab address with an expiry and no instrument', '/pricing-lab?expiry=1790000000000#top', '/markets?expiry=1790000000000&then=options&view=pricing#top'],
      ['the Pricing Lab address with no instrument and nothing else', '/pricing-lab', '/markets?then=options&view=pricing'],
      ['the Surface 3D address with an expiry and no instrument', '/broker/options-surface?expiry=1790000000000#top', '/markets?expiry=1790000000000&then=options&view=surface#top'],
    ])('takes %s straight to its view under the instrument, or to the list, with the expiry and fragment', async (_name, url, landsOn) => {
      const { router, ended, cancelled, failed } = await load(url);

      expect.soft(failed).toEqual([]);
      expect.soft(cancelled, 'redirects within the one navigation').toEqual([]);
      expect.soft(ended).toEqual([landsOn]);
      expect(router.url).toBe(landsOn);
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

    it.each([
      // The archive card left Data Lab, so its anchors lead to the Operations tab that holds each part of it, whichever page named them.
      ['/data-lab#option-archive-SPY', '/operations/option-archive#option-archive-SPY'],
      ['/data-lab?ticker=SPY#option-archive', '/operations/option-archive?ticker=SPY#option-archive'],
      ['/data-lab#option-archive-backup', '/operations/backups#option-archive-backup'],
      ['/data-lab#option-archive-restore', '/operations/backups#option-archive-restore'],
      ['/data-lab/export#option-archive-SPY', '/operations/option-archive#option-archive-SPY'],
      ['/data-lab?mode=build&ticker=SPY#option-archive-SPY', '/operations/option-archive?ticker=SPY#option-archive-SPY'],
      // Validate left Data Lab, so a mode or a path that names it goes to Operations with its scope.
      ['/data-lab?mode=validate', '/operations/data-checks/tradingview'],
      ['/data-lab?mode=validate&ticker=SPY&from=2026-01-02&to=2026-03-31', '/operations/data-checks/tradingview?ticker=SPY&from=2026-01-02&to=2026-03-31'],
      ['/data-lab/explore?ticker=QQQ&mode=validate', '/operations/data-checks/tradingview?ticker=QQQ'],
      ['/data-lab?mode=bogus&ticker=SPY', '/markets/SPY/chart?mode=bogus'],
      ['/data-lab?mode=explore&ticker=spy&from=2026-01-02&to=2026-02-27&trading-session=extended#top', '/markets/SPY/chart?from=2026-01-02&to=2026-02-27&session=extended#top'],
      ['/data-lab/explore?ticker=QQQ&from=2026-01-02&to=2026-02-27', '/markets/QQQ/chart?from=2026-01-02&to=2026-02-27'],
      // The old news page named no instrument in its address; a ticker in the query is the instrument, and with none it leads to the list.
      ['/research-lab/market/news?ticker=SPY&from=2026-01-02&to=2026-02-27#top', '/markets/SPY/chart/news?from=2026-01-02&to=2026-02-27#top'],
      ['/research-lab/market/news', '/markets'],
      // A symbol this app cannot hold is carried to the instrument's address, where it fails in place; it is never swapped for another.
      ['/data-lab/explore?ticker=%24%24%24', '/markets/$$$/chart'],
      // The Returns study moved under the instrument too: its window and session go with it, and the ticker becomes the path.
      ['/data-lab/returns?ticker=spy&from=2026-01-02&to=2026-02-27&trading-session=extended#top', '/markets/SPY/returns?from=2026-01-02&to=2026-02-27&session=extended#top'],
      ['/data-lab/returns', '/markets'],
      // Build dataset moved under the instrument, with the full scope its address carried: the ticker becomes the path, the
      // window and the retired `trading-session` ride as the place's context keys, and a saved setup it named still names it.
      ['/data-lab/export?ticker=spy&from=2026-01-02&to=2026-02-27&trading-session=extended&sessionId=6f9619ff-8b86-d011-b42d-00c04fc964ff#top', '/markets/SPY/data/build?from=2026-01-02&to=2026-02-27&sessionId=6f9619ff-8b86-d011-b42d-00c04fc964ff&session=extended#top'],
      ['/data-lab?mode=build&ticker=SPY&from=2026-01-02&to=2026-02-27', '/markets/SPY/data/build?from=2026-01-02&to=2026-02-27'],
      ['/data-lab/export', '/markets'],
    ])('lands the legacy Data Lab address %s on %s in one navigation', async (url, landsOn) => {
      const { router, ended, cancelled } = await load(url);

      expect(router.url).toBe(landsOn);
      expect(ended).toHaveLength(1);
      expect(cancelled).toEqual([]);
    });

    it.each([
      // The comparison's scope is the link's query; the page it started from does not matter.
      ['/data-quality?ticker=SPY&from=2026-01-02&to=2026-03-31', '/operations/data-checks/tradingview?ticker=SPY&from=2026-01-02&to=2026-03-31'],
      ['/data-lab/validate?ticker=AAPL&from=2026-04-15&to=2026-05-15', '/operations/data-checks/tradingview?ticker=AAPL&from=2026-04-15&to=2026-05-15'],
      ['/operations/data-checks?ticker=SPY#run', '/operations/data-checks/tradingview?ticker=SPY#run'],
      ['/golden-fixtures#cat-indicators', '/operations/math-checks#cat-indicators'],
      ['/research-lab/inspect/pre-flight?strategy=spy_ema_crossover', '/operations/data-checks/pre-flight?strategy=spy_ema_crossover'],
      ['/research-lab/strategy-preflight', '/operations/data-checks/pre-flight'],
      ['/research-lab/data-divergence?tf=1h', '/operations/data-checks/divergence?tf=1h'],
    ])('lands the old checks address %s straight on %s', async (url, landsOn) => {
      const { router, ended, cancelled } = await load(url);

      expect(router.url).toBe(landsOn);
      expect(ended).toHaveLength(1);
      expect(cancelled).toEqual([]);
    });

    // The chain moved into the instrument's Options tab: the old chain pages carry no symbol of their own, so one a bookmark
    // adds in its query is the instrument, and with none the address asks for one rather than guessing SPY.
    it.each([
      ['/options-lab/chain?symbol=SPY&expiry=4070984400000&source=archive&date=2026-10-09&at=1791576000000#top', '/markets/SPY/options/chain?expiry=4070984400000&source=archive&date=2026-10-09&at=1791576000000#top'],
      ['/options-lab/chain?ticker=qqq&source=snapshot', '/markets/QQQ/options/chain?source=snapshot'],
      // The old Chain (Live) page was the IBKR feed: its address opens Now · Live IBKR unless it names a source.
      ['/broker/options-chain?symbol=SPY&expiry=4070984400000', '/markets/SPY/options/chain?expiry=4070984400000&source=live'],
      ['/broker/options-chain?symbol=SPY', '/markets/SPY/options/chain?source=live'],
      ['/broker/options-chain?symbol=SPY&source=live', '/markets/SPY/options/chain?source=live'],
      ['/broker/options-chain?symbol=SPY&source=archive&date=2026-10-09', '/markets/SPY/options/chain?source=archive&date=2026-10-09'],
      ['/options-lab?ticker=SPY', '/markets/SPY/options/chain'],
      ['/options-lab/chain', '/markets?then=options'],
      ['/options-lab/chain?expiry=4070984400000&source=archive#top', '/markets?expiry=4070984400000&source=archive&then=options#top'],
      ['/broker/options-chain', '/markets?source=live&then=options'],
      ['/broker/options-chain?expiry=4070984400000#top', '/markets?expiry=4070984400000&source=live&then=options#top'],
      ['/options-lab', '/markets?then=options'],
    ])('lands the old chain address %s on %s in one navigation', async (url, landsOn) => {
      const { router, ended, cancelled, failed } = await load(url);

      expect(router.url).toBe(landsOn);
      expect(ended).toHaveLength(1);
      expect(cancelled).toEqual([]);
      expect(failed).toEqual([]);
    });

    // The redirect writes what the Markets list reads: an old Pricing, Surface or Chain (Live) address with no instrument must
    // reach the pick as the same view with the same keys, not as the Options tab's default.
    it.each([
      ['/pricing-lab?expiry=1790000000000&session=extended', ['/markets', 'SPY', 'options', 'pricing'], { expiry: '1790000000000', session: 'extended' }],
      ['/broker/options-surface?expiry=1790000000000', ['/markets', 'SPY', 'options', 'surface'], { expiry: '1790000000000' }],
      ['/broker/options-chain?expiry=1790000000000', ['/markets', 'SPY', 'options'], { expiry: '1790000000000', source: 'live' }],
      ['/options-lab/chain?source=archive&date=2026-10-09', ['/markets', 'SPY', 'options'], { source: 'archive', date: '2026-10-09' }],
    ])('hands the pick of %s its view and the keys it carried', async (url, commands, kept) => {
      const { router } = await load(url);
      const asked = router.parseUrl(router.url).queryParamMap;

      expect(openableTab(asked.get(PICK_TAB_KEY))).toBe('options');
      expect(optionsViewCommands('SPY', asked.get(PICK_VIEW_KEY))).toEqual(commands);
      expect(keysForPick(asked)).toEqual(kept);
    });

    it('fails in place on a Data check it does not have, rather than opening the first', async () => {
      const { router } = await load('/operations/data-checks/nothing');

      expect(router.url).toBe('/operations/data-checks/nothing');
      expect(routeTreeHasData(router.routerState.snapshot.root, 'notFound')).toBe(true);
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

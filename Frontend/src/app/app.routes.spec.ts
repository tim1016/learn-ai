import { Injector, runInInjectionContext } from '@angular/core';
import { TestBed } from '@angular/core/testing';
import {
  convertToParamMap,
  provideRouter,
  Router,
  UrlTree,
  type PartialMatchRouteSnapshot,
} from '@angular/router';
import { describe, expect, it } from 'vitest';

import { appConfig } from './app.config';
import { AlpacaBotControlExampleComponent } from './components/examples/alpaca-bot-control/alpaca-bot-control-example.component';
import { AlpacaAccountWorkspaceComponent } from './components/brokers/alpaca-workspace/alpaca-account-workspace.component';
import { DataLakeObservatoryComponent } from './components/data-lake-observatory/data-lake-observatory.component';
import { AlpacaSurfaceNotReadyTabComponent } from './components/brokers/alpaca-workspace/alpaca-surface-not-ready-tab.component';
import { AlpacaConfigurationPageComponent } from './components/brokers/alpaca-desk/configuration/alpaca-configuration-page.component';
import { AlpacaAccountListPageComponent } from './components/brokers/alpaca-desk/alpaca-account-list-page.component';
import { AlpacaDeskComponent } from './components/brokers/alpaca-desk/alpaca-desk.component';
import { BotPanelShellComponent } from './components/broker/v2-panel/panel-shell/bot-panel-shell.component';
import { BotsListPageComponent } from './components/broker/v2-panel/bots-list-page/bots-list-page.component';
import { BotGalleryPageComponent } from './components/broker/v2-panel/gallery/bot-gallery-page/bot-gallery-page.component';
import { alpacaSurfaceRedirectGuard } from './fleet/alpaca-surface-redirect.guard';
import { routes } from './app.routes';

describe('routes', () => {
  it('uses Strategy Lab as the canonical workbench and redirects legacy Engine Lab paths', () => {
    expect(routes.find((route) => route.path === 'strategy-lab')?.loadComponent).toBeDefined();
    expect(routes.find((route) => route.path === 'strategy-lab/runs/:id')?.redirectTo).toBeTypeOf('function');
    for (const path of ['engine', 'lean-engine', 'lean-lab']) {
      expect(routes.find((route) => route.path === path)).toMatchObject({
        redirectTo: 'strategy-lab',
        pathMatch: 'full',
      });
    }
    expect(routes.find((route) => route.path === 'engine/runs/:id')?.redirectTo).toBeTypeOf('function');
    expect(routes.find((route) => route.path === 'engine-docs')).toMatchObject({
      redirectTo: 'strategy-lab/docs',
      pathMatch: 'full',
    });
  });

  it.each([
    ['204', '/strategy-lab?run=204'],
    ['a b', '/strategy-lab?run=a%20b'],
  ])('redirects a persisted run URL (id %s) onto the one-page workbench', (id, expectedUrl) => {
    const route = routes.find((candidate) => candidate.path === 'strategy-lab/runs/:id');
    const redirect = route?.redirectTo;
    if (typeof redirect !== 'function') throw new Error('Strategy Lab run route is not a redirect.');

    TestBed.configureTestingModule({ providers: [provideRouter([])] });
    const params = { id };
    const queryParams = {};
    const redirectData: PartialMatchRouteSnapshot = {
      routeConfig: route ?? null,
      url: [],
      params,
      queryParams,
      fragment: null,
      data: {},
      outlet: 'primary',
      title: undefined,
      paramMap: convertToParamMap(params),
      queryParamMap: convertToParamMap(queryParams),
    };
    const tree = runInInjectionContext(TestBed.inject(Injector), () => redirect(redirectData));

    if (!(tree instanceof UrlTree)) throw new Error('Redirect did not produce a UrlTree.');
    expect(TestBed.inject(Router).serializeUrl(tree)).toBe(expectedUrl);
  });

  it.each([
    '/strategy-lab/runs/204/extra',
    '/engine/runs/204/extra',
  ])('does not treat %s as a persisted-run bookmark', async (url) => {
    // Without pathMatch: 'full' the run routes prefix-match, so a trailing
    // segment still fired the redirect with id 204 instead of falling through
    // to the wildcard — a URL that is not a run bookmark opened a run.
    TestBed.configureTestingModule({ providers: [provideRouter(routes)] });
    const router = TestBed.inject(Router);

    await router.navigateByUrl(url);

    expect(router.url).toBe('/data-lab/explore');
  });

  it('resolves the legacy engine/runs/:id bookmark onto the one-page workbench', async () => {
    // Angular's router does not chain a redirect target that is itself a
    // redirect route within one navigation (it re-matches the redirected URL
    // with redirects disallowed), so engine/runs/:id must redirect straight
    // to the final /strategy-lab?run=N destination rather than hopping
    // through strategy-lab/runs/:id. This is a full-router assertion, not a
    // route-config shape check, because that is exactly the distinction that
    // would otherwise hide the gap.
    TestBed.configureTestingModule({ providers: [provideRouter(routes)] });
    const router = TestBed.inject(Router);

    await router.navigateByUrl('/engine/runs/204');

    expect(router.url).toBe('/strategy-lab?run=204');
  });

  it.each([
    'options-lab',
    'strategy-lab',
  ])('marks %s as an intentionally full-bleed workspace', (path) => {
    expect(routes.find((route) => route.path === path)?.data).toMatchObject({ fullBleed: true });
  });

  it.each([
    ['/broker', '/brokers/alpaca'],
    ['/broker/accounts', '/brokers/alpaca'],
    ['/broker/accounts/account-1', '/brokers/alpaca'],
    ['/broker/account-monitor', '/brokers/alpaca'],
    ['/broker/reconciliation', '/brokers/alpaca'],
    ['/broker/orders', '/brokers/alpaca'],
    ['/broker/session-mirror', '/brokers/alpaca'],
    ['/broker/paper-run', '/brokers/alpaca'],
    ['/broker/instances', '/brokers/alpaca'],
    ['/broker/instances/bot-1', '/brokers/alpaca'],
    ['/broker/bots', '/brokers/alpaca'],
    ['/broker/bots/bot-1', '/brokers/alpaca'],
    ['/broker/offline-replay', '/brokers/alpaca'],
    ['/broker/deploy', '/brokers/alpaca'],
  ])('navigates the deprecated %s URL to %s', async (path, expectedUrl) => {
    TestBed.configureTestingModule({ providers: [provideRouter(routes)] });
    const router = TestBed.inject(Router);

    await router.navigateByUrl(path);

    expect(router.url).toBe(expectedUrl);
  });

  it('keeps the Clerk diagnostic gallery unlinked beneath the examples route', async () => {
    const route = routes.find((candidate) => candidate.path === 'examples/alpaca-bot-control');
    if (route?.loadComponent === undefined) throw new Error('Alpaca bot control example route is missing.');

    expect(await route.loadComponent()).toBe(AlpacaBotControlExampleComponent);
  });

  it('lazily loads the Data Lake Observatory and keeps it distinct from Data Lab', async () => {
    const route = routes.find((candidate) => candidate.path === 'data-lake');
    if (route?.loadComponent === undefined) throw new Error('Data Lake Observatory route is missing.');

    expect(await route.loadComponent()).toBe(DataLakeObservatoryComponent);
    expect(routes.find((candidate) => candidate.path === 'data-lab')?.loadChildren).toBeDefined();
  });

  it('retires the broker-wide surface choosers onto the account list', async () => {
    // A surface belongs to an account (ADR 0064 Decision 2), so these two
    // bookmarks have no page of their own any more — they land on the one
    // page that can name an account, rather than 404-ing.
    for (const path of ['brokers/alpaca/bots', 'brokers/alpaca/gallery']) {
      const route = routes.find((candidate) => candidate.path === path);

      expect(route).toMatchObject({ redirectTo: '/brokers/alpaca', pathMatch: 'full' });
      expect(route?.loadComponent).toBeUndefined();
    }
  });

  it('retires the desk surface hints through the redirect guard', () => {
    const desk = routes.find((candidate) => candidate.path === 'brokers/alpaca');

    expect(desk?.canActivate).toContain(alpacaSurfaceRedirectGuard);
  });

  it('serves the account list — and its ?deploy intent — from the one broker route', async () => {
    const list = routes.find((candidate) => candidate.path === 'brokers/alpaca');

    // `?deploy` is a query on this same route, not a page of its own: the
    // list is the deploy hand-off's account-selection step.
    expect(await list?.loadComponent?.()).toBe(AlpacaAccountListPageComponent);
    expect(routes.some((candidate) => candidate.path === 'brokers/alpaca?deploy')).toBe(false);
  });

  it.each([
    ['/brokers/alpaca?surface=bots'],
    ['/brokers/alpaca?surface=gallery'],
    ['/brokers/alpaca/bots'],
    ['/brokers/alpaca/gallery'],
  ])('lands %s on the account list, in however many hops it takes', async (path) => {
    // The guard still folds `?surface=bots` onto `/brokers/alpaca/bots`,
    // which now redirects back to the list. The chain has to terminate on the
    // list rather than loop or strand the bookmark.
    TestBed.configureTestingModule({ providers: [provideRouter(routes)] });
    const router = TestBed.inject(Router);

    await router.navigateByUrl(path);

    expect(router.url).toBe('/brokers/alpaca');
  });

  describe('the account workspace (ADR 0064)', () => {
    const workspace = routes.find(
      (candidate) => candidate.path === 'brokers/alpaca/clerks/:clerkId',
    );
    const account = workspace?.children?.find((child) => child.path === 'accounts/:accountId');

    it('nests every tab — account-scoped and lane-scoped — under one workspace route', async () => {
      expect(await workspace?.loadComponent?.()).toBe(AlpacaAccountWorkspaceComponent);
      // The shell sits at the clerk level because two tabs name no account:
      // Configuration is lane-scoped (FR-092), and a lane with no confirmed
      // account still keeps its Bots and Gallery tabs (FR-096).
      expect(workspace?.children?.map((child) => child.path)).toEqual([
        'configuration', 'bots', 'gallery', 'accounts/:accountId', '',
      ]);
      // Overview is the account's empty child, so the account's own URL opens
      // it and the canonical URLs are unchanged. `bots/:sid` is declared
      // before the `bots` tab it nests under, so the longer path matches
      // without relying on the router backtracking between siblings. Deploy
      // is one of the five tabs (ADR 0064 Decision 1 extended), routed
      // inline rather than opened as an overlay.
      expect(account?.children?.map((child) => child.path)).toEqual([
        'bots/:sid', 'bots', 'gallery', 'deploy', '',
      ]);
    });

    it('declares full-bleed once, on the workspace itself, so the header never moves', () => {
      // The header and tab strip are the workspace's chrome: a per-tab
      // `fullBleed` gave the shell's page inset to some tabs and not others,
      // which shifted the header when the operator opened Gallery. One flag
      // on the parent is what makes every tab agree.
      expect(workspace?.data).toMatchObject({ fullBleed: true });
      for (const child of [...(workspace?.children ?? []), ...(account?.children ?? [])]) {
        expect(child.data?.['fullBleed']).toBeUndefined();
      }
    });

    it('opens a lane deep link without a tab on the one tab it can always serve', () => {
      // Configuration access needs no confirmed binding, so it is the lane's
      // own home — and the operator's way to bind an account.
      expect(workspace?.children?.find((child) => child.path === '')).toMatchObject({
        redirectTo: 'configuration',
        pathMatch: 'full',
      });
    });

    it.each([
      ['configuration', AlpacaConfigurationPageComponent],
      ['bots', AlpacaSurfaceNotReadyTabComponent],
      ['gallery', AlpacaSurfaceNotReadyTabComponent],
    ])('loads the lane-scoped %s tab', async (path, expectedComponent) => {
      const route = workspace?.children?.find((candidate) => candidate.path === path);
      if (route === undefined) throw new Error(`Workspace tab ${path} is missing.`);

      expect(route.redirectTo).toBeUndefined();
      expect(route.canActivate).toBeUndefined();
      expect(await route.loadComponent?.()).toBe(expectedComponent);
    });

    it.each([
      ['bots', 'bots'],
      ['gallery', 'gallery'],
    ])('tells the not-ready %s tab which surface it explains', (path, surface) => {
      expect(workspace?.children?.find((child) => child.path === path)?.data).toMatchObject({
        surface,
      });
    });

    it.each([
      ['', AlpacaDeskComponent, 'the account overview'],
      ['bots', BotsListPageComponent, 'the bots roster'],
      ['gallery', BotGalleryPageComponent, 'the gallery'],
    ])(
      'keeps the %s tab on its own operational component — never a redirect to configuration',
      async (path, expectedComponent, _surfaceLabel) => {
        // Regression: an operator reported a clerk-scoped Bots URL landing on
        // the broker configuration page. No such redirect exists in the table;
        // this pins that the canonical operational routes stay loadComponent
        // routes with no redirectTo and no canActivate retargeting.
        const route = account?.children?.find((candidate) => candidate.path === path);
        if (route === undefined) throw new Error(`Workspace tab ${path} is missing.`);

        expect(route.redirectTo).toBeUndefined();
        expect(route.canActivate).toBeUndefined();
        expect(await route.loadComponent?.()).toBe(expectedComponent);
      },
    );

    it.each([
      ['/brokers/alpaca/clerks/clrk_spec/accounts/PA9', 'the Overview tab'],
      ['/brokers/alpaca/clerks/clrk_spec/accounts/PA9/bots', 'the Bots tab'],
      ['/brokers/alpaca/clerks/clrk_spec/accounts/PA9/gallery', 'the Gallery tab'],
    ])('carries clerk and account identity into %s', async (url) => {
      // Asserted through the app's own router configuration, not a local one:
      // a non-empty child only inherits its parent's params under
      // `paramsInheritanceStrategy: 'always'`, and without it the Bots and
      // Gallery tabs would render without the lane their URL names (FR-092).
      TestBed.configureTestingModule({ providers: appConfig.providers });
      const router = TestBed.inject(Router);

      await router.navigateByUrl(url);

      let route = router.routerState.snapshot.root;
      while (route.firstChild !== null) route = route.firstChild;
      expect(route.params).toMatchObject({ clerkId: 'clrk_spec', accountId: 'PA9' });
      expect(route.data).toMatchObject({ broker: 'alpaca' });
    });

    it.each([
      ['/brokers/alpaca/clerks/clrk_spec/configuration', 'Configuration'],
      ['/brokers/alpaca/clerks/clrk_spec/bots', 'the not-ready Bots tab'],
      ['/brokers/alpaca/clerks/clrk_spec/gallery', 'the not-ready Gallery tab'],
    ])('carries clerk identity — and no account — into %s', async (url) => {
      TestBed.configureTestingModule({ providers: appConfig.providers });
      const router = TestBed.inject(Router);

      await router.navigateByUrl(url);

      let route = router.routerState.snapshot.root;
      while (route.firstChild !== null) route = route.firstChild;
      expect(route.params).toMatchObject({ clerkId: 'clrk_spec' });
      expect(route.params['accountId']).toBeUndefined();
    });

    it("puts a bot's own page inside the workspace, not beside it", async () => {
      // It used to be a top-level route declared before the workspace so the
      // deeper URL would win the match. Inside, it is the account's own child
      // and the workspace header and tab strip stay above it.
      expect(
        routes.some(
          (candidate) =>
            candidate.path === 'brokers/alpaca/clerks/:clerkId/accounts/:accountId/bots/:sid',
        ),
      ).toBe(false);
      const panel = account?.children?.find((child) => child.path === 'bots/:sid');

      expect(await panel?.loadComponent?.()).toBe(BotPanelShellComponent);
    });

    it.each([
      ['/brokers/alpaca/clerks/clrk_spec/accounts/PA9/bots/sid-1', 'bots/:sid'],
      ['/brokers/alpaca/clerks/clrk_spec/accounts/PA9/bots', 'bots'],
    ])('resolves %s to the %s child', async (url, path) => {
      // The two siblings differ by one segment, so this pins that both still
      // resolve — the Bots tab and one bot's page, in the app's own table.
      TestBed.configureTestingModule({ providers: appConfig.providers });
      const router = TestBed.inject(Router);

      await router.navigateByUrl(url);

      let route = router.routerState.snapshot.root;
      while (route.firstChild !== null) route = route.firstChild;
      expect(route.routeConfig?.path).toBe(path);
    });
  });
});

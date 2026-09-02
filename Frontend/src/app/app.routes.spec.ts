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

import { AlpacaBotControlExampleComponent } from './components/examples/alpaca-bot-control/alpaca-bot-control-example.component';
import { DataLakeObservatoryComponent } from './components/data-lake-observatory/data-lake-observatory.component';
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

    expect(router.url).toBe('/data-lab');
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
    'brokers/:broker/accounts/:accountId/gallery',
    'brokers/:broker/accounts/:accountId/bots/:sid',
  ])('marks %s as an intentionally full-bleed workspace', (path) => {
    expect(routes.find((route) => route.path === path)?.data).toMatchObject({ fullBleed: true });
  });

  it('redirects historical Alpaca Deploy URLs into the desk drawer', () => {
    for (const path of ['brokers/alpaca/deploy', 'brokers/alpaca/accounts/:accountId/deploy']) {
      expect(routes.find((candidate) => candidate.path === path)).toMatchObject({
        redirectTo: 'brokers/alpaca?deploy',
        pathMatch: 'full',
      });
    }
  });

  it('keeps unscoped broker bot surfaces behind account-resolving guards', () => {
    for (const path of ['brokers/:broker/bots', 'brokers/:broker/gallery']) {
      const route = routes.find((candidate) => candidate.path === path);
      expect(route?.canActivate).toHaveLength(1);
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
    ['broker/deploy', 'brokers/alpaca?deploy'],
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

  it('lazily loads the Data Lake Observatory and keeps it distinct from Data Lab', async () => {
    const route = routes.find((candidate) => candidate.path === 'data-lake');
    if (route?.loadComponent === undefined) throw new Error('Data Lake Observatory route is missing.');

    expect(await route.loadComponent()).toBe(DataLakeObservatoryComponent);
    expect(routes.find((candidate) => candidate.path === 'data-lab')?.loadComponent).toBeDefined();
  });
});

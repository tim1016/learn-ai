import { Injector, runInInjectionContext } from '@angular/core';
import { TestBed } from '@angular/core/testing';
import {
  ActivatedRouteSnapshot,
  Router,
  UrlTree,
  convertToParamMap,
  provideRouter,
} from '@angular/router';
import { describe, expect, it, vi } from 'vitest';

import { testLane } from './fleet-directory-testing';
import { FleetDirectoryService } from './fleet-directory.service';
import { brokerClerkRedirectGuard } from './broker-clerk-redirect.guard';

function routeFor(params: Record<string, string>): ActivatedRouteSnapshot {
  const route = new ActivatedRouteSnapshot();
  Object.defineProperty(route, 'paramMap', { value: convertToParamMap(params) });
  return route;
}

describe('brokerClerkRedirectGuard', () => {
  function runGuard(
    params: Record<string, string>,
    fleet: Pick<FleetDirectoryService, 'ensureLoaded' | 'laneForAccount'>,
  ) {
    TestBed.configureTestingModule({
      providers: [
        provideRouter([]),
        { provide: FleetDirectoryService, useValue: fleet },
      ],
    });
    const route = routeFor(params);
    const guard = brokerClerkRedirectGuard('/bots');
    const state = TestBed.inject(Router).routerState.snapshot;
    return runInInjectionContext(TestBed.inject(Injector), () =>
      guard(route, state),
    );
  }

  it('waits for a cold directory load and redirects only an exact ready account lane', async () => {
    const lane = testLane();
    const ensureLoaded = vi.fn(() => Promise.resolve({ observed_at_ms: 1, clerks: [lane] }));
    const laneForAccount = vi.fn(() => lane);
    const result = await runGuard(
      { broker: 'alpaca', accountId: lane.provider_summary?.confirmed_account_id ?? '' },
      { ensureLoaded, laneForAccount },
    );

    if (!(result instanceof UrlTree)) throw new Error('Exact lane did not redirect.');
    expect(ensureLoaded).toHaveBeenCalledOnce();
    expect(TestBed.inject(Router).serializeUrl(result)).toBe(
      `/brokers/alpaca/clerks/${lane.clerk_id}/accounts/${lane.provider_summary?.confirmed_account_id}/bots`,
    );
  });

  it.each([
    ['unknown account', { broker: 'alpaca', accountId: 'missing' }, undefined],
    ['wrong provider', { broker: 'ibkr', accountId: 'account-1' }, testLane({ broker: 'ibkr' })],
    ['retired lane', { broker: 'alpaca', accountId: 'account-1' }, testLane({ lifecycle_state: 'retired' })],
  ])('fails in place for a %s compatibility URL', async (_name, params, lane) => {
    const result = await runGuard(params, {
      ensureLoaded: () => Promise.resolve({ observed_at_ms: 1, clerks: [] }),
      laneForAccount: () => lane,
    });

    expect(result).toBe(true);
  });

  it('fails in place when the cold directory request rejects', async () => {
    const result = await runGuard(
      { broker: 'alpaca', accountId: 'account-1' },
      {
        ensureLoaded: () => Promise.reject(new Error('offline')),
        laneForAccount: () => testLane(),
      },
    );

    expect(result).toBe(true);
  });

  it('fails in place when a malformed compatibility route omits its broker', async () => {
    const result = await runGuard(
      { accountId: 'account-1' },
      {
        ensureLoaded: () => Promise.resolve({ observed_at_ms: 1, clerks: [] }),
        laneForAccount: () => testLane(),
      },
    );

    expect(result).toBe(true);
  });
});

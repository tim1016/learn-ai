import { TestBed } from '@angular/core/testing';
import { ActivatedRoute, convertToParamMap } from '@angular/router';
import { BehaviorSubject } from 'rxjs';
import { describe, expect, it, vi } from 'vitest';

import { BrokersService } from '../../../services/brokers.service';
import { provideFleetDirectory, testLane } from '../../../fleet/fleet-directory-testing';
import { AlpacaDeskAccountDataService } from './alpaca-desk-account-data.service';

/** `paramMap` is a `BehaviorSubject`, not a one-shot `of(...)`, so a test can
 * push a second route (a navigation) after the service has already
 * constructed. `snapshot.paramMap` stays pinned to the INITIAL value — that
 * is genuinely all `ActivatedRoute.snapshot` is: the value at the moment the
 * route resolved, not a live view — matching what the service actually reads
 * from it (only `toSignal`'s `initialValue`, before the observable's first
 * emission). */
function activatedRoute(clerkId: string, accountId: string) {
  const initial = convertToParamMap({ clerkId, accountId });
  const paramMap$ = new BehaviorSubject(initial);
  return {
    provider: {
      provide: ActivatedRoute,
      useValue: { paramMap: paramMap$, snapshot: { paramMap: initial } },
    },
    paramMap$,
  };
}

/** Never resolves: this suite only exercises `fence`, and letting the
 * `account` resource's `getAccount` call settle (or reject) is irrelevant
 * noise for it. */
function neverAccount() {
  return vi.fn(() => new Promise<never>(() => undefined));
}

/**
 * `AlpacaDeskAccountDataService` is the ONE place `openLaneFence(freeze,
 * source)` is wired for this feature (#2106) — every consumer spec
 * (`alpaca-order-entry`, `alpaca-sqlite-custody`, `account-desk-transaction-
 * history`, `alpaca-operator-lens`, `alpaca-trader-lens`, `alpaca-desk`)
 * fakes `AlpacaDeskAccountDataService` and feeds `fence` as a literal input,
 * so none of them exercise this wiring. This spec renders the service with a
 * REAL, reactive `FleetDirectoryService` double (`provideFleetDirectory`,
 * not a static stub) and proves BOTH halves of the one property that makes
 * this a fix rather than a relabeling: `fence()` must not move on a
 * directory refresh (re-deriving too MUCH — the #2106 bug), and it MUST move
 * on a route change (re-deriving too LITTLE — a constant or otherwise
 * route-blind `source` would mint every command on the desk the operator
 * first opened, regardless of which lane they navigated to since). Neither
 * case alone proves `source` is genuinely route-keyed; only both together do.
 */
describe('AlpacaDeskAccountDataService', () => {
  it.each([['pa1', true], ['OTHER', false]])('checks the broker account against canonical route %s', async (accountId, accepted) => {
    TestBed.configureTestingModule({
      providers: [
        provideFleetDirectory({ observed_at_ms: 1, clerks: [testLane({ clerk_id: 'clrk_spec' })] }),
        activatedRoute('clrk_spec', accountId).provider,
        { provide: BrokersService, useValue: { getAccount: vi.fn().mockResolvedValue({ account_id: 'PA1' }) } },
        AlpacaDeskAccountDataService,
      ],
    });
    const service = TestBed.inject(AlpacaDeskAccountDataService);
    TestBed.tick();
    await vi.waitFor(() => expect(service.account.isLoading()).toBe(false));
    expect(service.account.hasValue()).toBe(accepted);
  });

  it('does not re-derive its command fence when the directory refreshes on the same route', () => {
    const directory = provideFleetDirectory({
      observed_at_ms: 1_757_000_000_000,
      clerks: [testLane({ clerk_id: 'clrk_spec', effective_binding_generation: 3, routing_epoch: 4 })],
    });
    TestBed.configureTestingModule({
      providers: [
        directory,
        activatedRoute('clrk_spec', 'PA1').provider,
        { provide: BrokersService, useValue: { getAccount: neverAccount() } },
        AlpacaDeskAccountDataService,
      ],
    });
    const service = TestBed.inject(AlpacaDeskAccountDataService);
    TestBed.tick();

    expect(service.fence()).toEqual({ bindingGeneration: 3, routingEpoch: 4 });

    // The directory rebinds on the SAME route — exactly what #2068's
    // mitigating `FleetDirectoryService.refresh()` (a stale-generation
    // refusal) or an unrelated background poll does mid-session.
    directory.rebind({
      observed_at_ms: 1_757_000_000_001,
      clerks: [testLane({ clerk_id: 'clrk_spec', effective_binding_generation: 99, routing_epoch: 55 })],
    });
    TestBed.tick();

    expect(service.fence()).toEqual({ bindingGeneration: 3, routingEpoch: 4 });
  });

  it('re-derives its command fence when the operator navigates to a different clerk', () => {
    const directory = provideFleetDirectory({
      observed_at_ms: 1_757_000_000_000,
      clerks: [
        testLane({ clerk_id: 'clrk_spec', effective_binding_generation: 3, routing_epoch: 4 }),
        testLane({ clerk_id: 'clrk_other', effective_binding_generation: 9, routing_epoch: 2 }),
      ],
    });
    const route = activatedRoute('clrk_spec', 'PA1');
    TestBed.configureTestingModule({
      providers: [
        directory,
        route.provider,
        { provide: BrokersService, useValue: { getAccount: neverAccount() } },
        AlpacaDeskAccountDataService,
      ],
    });
    const service = TestBed.inject(AlpacaDeskAccountDataService);
    TestBed.tick();

    expect(service.fence()).toEqual({ bindingGeneration: 3, routingEpoch: 4 });

    // A real navigation: the route now names a different clerk entirely, not
    // a directory refresh of the same one.
    route.paramMap$.next(convertToParamMap({ clerkId: 'clrk_other', accountId: 'PA2' }));
    TestBed.tick();

    expect(service.fence()).toEqual({ bindingGeneration: 9, routingEpoch: 2 });
  });
});

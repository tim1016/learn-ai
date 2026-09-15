import { TestBed } from '@angular/core/testing';
import { ActivatedRoute, convertToParamMap } from '@angular/router';
import { of } from 'rxjs';
import { describe, expect, it, vi } from 'vitest';

import { BrokersService } from '../../../services/brokers.service';
import { provideFleetDirectory, testLane } from '../../../fleet/fleet-directory-testing';
import { AlpacaDeskAccountDataService } from './alpaca-desk-account-data.service';

function activatedRoute(clerkId: string, accountId: string) {
  const paramMap = convertToParamMap({ clerkId, accountId });
  return {
    provide: ActivatedRoute,
    useValue: { paramMap: of(paramMap), snapshot: { paramMap } },
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
 * not a static stub) and asserts the one property that makes this a fix
 * rather than a relabeling: a directory refresh on the SAME route does not
 * move `fence()`.
 */
describe('AlpacaDeskAccountDataService', () => {
  it('does not re-derive its command fence when the directory refreshes on the same route', () => {
    const directory = provideFleetDirectory({
      observed_at_ms: 1_757_000_000_000,
      clerks: [testLane({ clerk_id: 'clrk_spec', effective_binding_generation: 3, routing_epoch: 4 })],
    });
    TestBed.configureTestingModule({
      providers: [
        directory,
        activatedRoute('clrk_spec', 'PA1'),
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
});

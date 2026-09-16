import { describe, expect, it } from 'vitest';

import { provideFleetDirectory, testLane, TEST_CLERK_ID } from './fleet-directory-testing';
import type { FleetDirectoryService } from './fleet-directory.service';

/** The double is evidence for the binding-generation fence, so it must be able
 * to do the one thing the fence exists to survive: rebind mid-session. A double
 * that cannot rebind makes every freeze-at-open test unfalsifiable. */
describe('the fleet directory double', () => {
  it('reports a rebinding once the directory is replaced and refreshed', async () => {
    const directory = provideFleetDirectory();
    const service = directory.useValue as unknown as FleetDirectoryService;

    expect(service.lane('alpaca', TEST_CLERK_ID)?.effective_binding_generation).toBe(3);

    // `rebind()` only stages the replacement, exactly like the real
    // `/api/broker-clerks` returning something new on the next request: it
    // is not visible to `lane()`/`ensureLoaded()` until something actually
    // re-fetches. `refresh()` never promoted on its own before this — the
    // real service's `ensureLoaded()` is a permanent no-op once loaded, so
    // a double that let a bare rebind show up instantly could not tell that
    // apart from a real refresh.
    directory.rebind({
      observed_at_ms: 1_757_000_000_001,
      clerks: [testLane({ effective_binding_generation: 4, routing_epoch: 5 })],
    });
    expect(service.lane('alpaca', TEST_CLERK_ID)?.effective_binding_generation).toBe(3);

    await service.refresh();

    expect(service.lane('alpaca', TEST_CLERK_ID)?.effective_binding_generation).toBe(4);
    expect(service.lane('alpaca', TEST_CLERK_ID)?.routing_epoch).toBe(5);
    expect(service.value()?.observed_at_ms).toBe(1_757_000_000_001);
  });

  it('keeps the default single-ready-lane response for callers that never rebind', () => {
    const service = provideFleetDirectory().useValue as unknown as FleetDirectoryService;
    expect(service.lanesOf('alpaca')).toHaveLength(1);
    expect(service.lanesOf('tradier')).toHaveLength(0);
  });
});

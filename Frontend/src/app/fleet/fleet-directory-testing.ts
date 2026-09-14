/** Test seam for the fleet directory: one ready Alpaca lane by default.
 *
 * Specs provide `provideFleetDirectory()` to keep lane-resolution cheap and
 * deterministic; the fixture mirrors the wire shape of `/api/broker-clerks`
 * (backend `ClerkDescriptor.public_fields()`). */
import { FleetDirectoryService } from './fleet-directory.service';
import type { FleetDirectoryResponse, LaneDescriptor } from './fleet-directory.types';

export const TEST_CLERK_ID = 'clrk_spec0000000000000000aa';
export const TEST_ACCOUNT_ID = 'abcdef01-1234-abcd-5678-ef0123456789';

export function testLane(
  overrides: Partial<LaneDescriptor> = {},
): LaneDescriptor {
  return {
    broker: 'alpaca',
    clerk_id: TEST_CLERK_ID,
    display_label: 'Paper',
    lifecycle_state: 'ready',
    volume_id: 'vol_test',
    last_seen_at_ms: 1_757_000_000_000,
    routing_epoch: 4,
    effective_binding_generation: 3,
    capabilities: [
      'account_read',
      'bot_panel_read',
      'bot_action',
      'configuration_manage',
      'deploy',
      'gallery_read',
    ],
    provider_summary: {
      provider_id: 'alpaca',
      adapter_version: 'alpaca-fleet.3',
      confirmed_account_id: TEST_ACCOUNT_ID,
      confirmed_binding_generation: 3,
      endpoint_mode: 'paper',
      authority_state: 'real_paper',
    },
    observed_at_ms: 1_757_000_000_000,
    ...overrides,
  };
}

export interface FleetDirectoryDouble {
  readonly provide: typeof FleetDirectoryService;
  readonly useValue: Partial<FleetDirectoryService>;
  /** Replace the served directory, as a coordinator rebinding would. */
  rebind(next: FleetDirectoryResponse): void;
}

export function provideFleetDirectory(
  initial: FleetDirectoryResponse = {
    observed_at_ms: 1_757_000_000_000,
    clerks: [testLane()],
  },
): FleetDirectoryDouble {
  let response = initial;
  return {
    provide: FleetDirectoryService,
    useValue: {
      value: () => response,
      error: () => undefined,
      isLoading: () => false,
      lanesOf: (broker: string) => response.clerks.filter((lane) => lane.broker === broker),
      lane: (broker: string, clerkId: string) =>
        response.clerks.find(
          (candidate) => candidate.broker === broker && candidate.clerk_id === clerkId,
        ),
      laneForAccount: (broker: string, accountId: string) =>
        response.clerks.find(
          (candidate) =>
            candidate.broker === broker &&
            candidate.provider_summary?.confirmed_account_id?.toLowerCase() ===
              accountId.trim().toLowerCase(),
        ),
      refresh: () => Promise.resolve(response),
      ensureLoaded: () => Promise.resolve(response),
    } as Partial<FleetDirectoryService>,
    rebind(next: FleetDirectoryResponse) {
      response = next;
    },
  };
}

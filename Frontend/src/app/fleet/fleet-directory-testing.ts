/** Test seam for the fleet directory: one ready Alpaca lane by default.
 *
 * Specs provide `provideFleetDirectory()` to keep lane-resolution cheap and
 * deterministic; the fixture mirrors the wire shape of `/api/broker-clerks`
 * (backend `ClerkDescriptor.public_fields()`). */
import { signal } from '@angular/core';
import { FleetDirectoryService } from './fleet-directory.service';
import { laneDisplayName } from './fleet-directory.types';
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
  // A real signal, not a reassigned closure variable: `lane()` must be a
  // genuine reactive read so a consumer reading it inside a tracked context
  // (an `effect`, a `computed`, a `linkedSignal` computation) actually
  // subscribes. A plain-variable double let an `untracked()` guard around
  // such a read go unverified by every spec that uses this fixture — the
  // guard could be deleted and no test would notice (#2068 fix round 1).
  //
  // `visible` is what every accessor (`value`/`lanesOf`/`lane`/
  // `laneForAccount`/`ensureLoaded`) reads — it models the real service's
  // cached `response` signal, which only ever changes when a `load()`
  // resolves. `staged` models the server-side directory the real
  // `/api/broker-clerks` would return on the next request; `rebind()` only
  // updates `staged`. Only `refresh()` promotes `staged` into `visible`,
  // exactly as the real `FleetDirectoryService.ensureLoaded()` is a no-op
  // once loaded while `refresh()` re-fetches. A double whose `rebind()`
  // wrote straight to `visible` made `ensureLoaded()` see a rebind
  // instantly, which the real service can never do.
  const visible = signal(initial);
  let staged = initial;
  return {
    provide: FleetDirectoryService,
    useValue: {
      value: () => visible(),
      error: () => undefined,
      isLoading: () => false,
      lanesOf: (broker: string) => visible().clerks.filter((lane) => lane.broker === broker),
      lane: (broker: string, clerkId: string) =>
        visible().clerks.find(
          (candidate) => candidate.broker === broker && candidate.clerk_id === clerkId,
        ),
      displayNameOf: (broker: string, clerkId: string) => {
        // Through the real `laneDisplayName`, against the real sibling set: a
        // double that returned `lane.display_label` would hide every
        // nickname-and-collision case the surfaces above it exist to render.
        const lanes = visible().clerks.filter((candidate) => candidate.broker === broker);
        const lane = lanes.find((candidate) => candidate.clerk_id === clerkId);
        return lane === undefined ? null : laneDisplayName(lane, lanes);
      },
      laneForAccount: (broker: string, accountId: string) =>
        visible().clerks.find(
          (candidate) =>
            candidate.broker === broker &&
            candidate.provider_summary?.confirmed_account_id?.toLowerCase() ===
              accountId.trim().toLowerCase(),
        ),
      refresh: () => {
        visible.set(staged);
        return Promise.resolve(visible());
      },
      ensureLoaded: () => Promise.resolve(visible()),
    } as Partial<FleetDirectoryService>,
    rebind(next: FleetDirectoryResponse) {
      staged = next;
    },
  };
}

import type { FleetCapability } from './resource-target';

/** The clerk fleet directory's wire shapes (PRD §10.1, backend
 * `ClerkDescriptor.public_fields()`). The directory is broker-neutral and
 * read-only; `provider_summary` is provider-authored and never interpreted
 * for financial meaning by the frontend. */

export interface LaneProviderSummary {
  provider_id?: string;
  adapter_version?: string;
  confirmed_account_id?: string | null;
  confirmed_binding_generation?: number | null;
  endpoint_mode?: string | null;
  authority_state?: string | null;
  detail?: unknown;
}

export interface LaneDescriptor {
  broker: string;
  clerk_id: string;
  display_label: string;
  lifecycle_state: string;
  volume_id: string;
  last_seen_at_ms: number | null;
  routing_epoch: number | null;
  effective_binding_generation: number | null;
  capabilities: FleetCapability[];
  provider_summary: LaneProviderSummary | null;
  observed_at_ms: number;
}

export interface FleetDirectoryResponse {
  observed_at_ms: number;
  clerks: LaneDescriptor[];
}

/** A lane is routable for execution when its registry projection says it is
 * ready with a confirmed binding; every other state renders in place with
 * its lifecycle, never redirects to another lane (FR-096). */
export function laneIsReady(lane: LaneDescriptor): boolean {
  return lane.lifecycle_state === 'ready';
}

/** The account a lane's confirmed binding serves, when it serves one. */
export function laneConfirmedAccount(lane: LaneDescriptor): string | null {
  const accountId = lane.provider_summary?.confirmed_account_id;
  return typeof accountId === 'string' && accountId.length > 0 ? accountId : null;
}

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
  /** The confirmed account's nickname (ADR 0064 Decision 5), when its lane
   * has one set. Operator prose, not a backend identifier — same footing as
   * `display_label` below, never piped through `receiptLabel`. */
  account_nickname?: string | null;
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

/** One lane's resolved display name (ADR 0064 Decision 5): its own nickname,
 * or its lane label until one is set. `disambiguator` is this lane's own
 * `display_label`, present only when `name` collides — trimmed and
 * case-insensitively — with another lane's in `allLanes`; otherwise `null`. */
export interface LaneDisplayName {
  readonly name: string;
  readonly disambiguator: string | null;
}

/** The raw name one lane would show before duplicate checking: its trimmed
 * nickname when it has a non-blank one, otherwise its lane label. */
function laneRawDisplayName(lane: LaneDescriptor): string {
  const nickname = lane.provider_summary?.account_nickname;
  return typeof nickname === 'string' && nickname.trim().length > 0
    ? nickname.trim()
    : lane.display_label;
}

/** A lane's display name, plus its own label to show beside it when that
 * name is shared with another lane in the directory.
 *
 * Names are compared trimmed and case-insensitively across every lane in
 * `allLanes`. Nothing here refuses a duplicate — nicknames live on each
 * lane's own volume, so no single writer sees every lane to enforce
 * cross-lane uniqueness (ADR 0064 "Considered options"). Showing each
 * colliding lane's own label beside its name is how the two stay
 * distinguishable instead. */
export function laneDisplayName(
  lane: LaneDescriptor,
  allLanes: readonly LaneDescriptor[],
): LaneDisplayName {
  const name = laneRawDisplayName(lane);
  const normalized = name.toLowerCase();
  const isShared = allLanes.some(
    (other) =>
      other.clerk_id !== lane.clerk_id
      && laneRawDisplayName(other).toLowerCase() === normalized,
  );
  return { name, disambiguator: isShared ? lane.display_label : null };
}

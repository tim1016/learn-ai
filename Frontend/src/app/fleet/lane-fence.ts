/** The binding-generation fence, frozen at action open (#2068, decision 10).
 *
 * The coordinator can only refuse a generation the client *sends*
 * (`service.py:1283-1292`, `routing.py:328-343` — both `is not None`-gated). A
 * click-time read always sends the current one, so the fence is structurally
 * unable to fire: it protects nothing unless the value is captured when the
 * operator was shown the lane and carried unchanged to submission.
 *
 * A null fence is not a safe default. `commandContextOf` omits
 * `expected_effective_binding_generation` when the generation is null
 * (`resource-target.ts:139-141`), so an unfenced command routes with no
 * generation check at all. `laneFenceIsEnforceable` names that state so a
 * surface can refuse rather than dispatch blind.
 */

import type { LaneDescriptor } from './fleet-directory.types';
import { resourceTarget, type ResourceTarget } from './resource-target';

export interface LaneFence {
  readonly bindingGeneration: number | null;
  readonly routingEpoch: number | null;
}

/** Capture what the rendered lane said, at the moment it was rendered. */
export function freezeLaneFence(lane: LaneDescriptor | undefined): LaneFence {
  return Object.freeze({
    bindingGeneration: lane?.effective_binding_generation ?? null,
    routingEpoch: lane?.routing_epoch ?? null,
  });
}

/** Whether this fence can actually be enforced by the coordinator. */
export function laneFenceIsEnforceable(fence: LaneFence): boolean {
  return fence.bindingGeneration !== null;
}

/** Whether the lane has moved out from under a frozen fence. A lane that
 * vanished from the directory counts as drift: it cannot be proven unchanged. */
export function laneFenceDrifted(frozen: LaneFence, lane: LaneDescriptor | undefined): boolean {
  if (lane === undefined) return true;
  const current = freezeLaneFence(lane);
  return (
    current.bindingGeneration !== frozen.bindingGeneration ||
    current.routingEpoch !== frozen.routingEpoch
  );
}

/** The one sentence a surface shows when a frozen command met a rebound lane. */
export const LANE_FENCE_CONFLICT_MESSAGE =
  'This clerk lane was rebound while the action was open, so the command was not sent. ' +
  'Reopen the action to reissue it against the lane as it stands now.';

/** Whether a target still matches the fence it was frozen against. */
export function targetMatchesFence(target: ResourceTarget, fence: LaneFence): boolean {
  return (
    target.bindingGeneration === fence.bindingGeneration &&
    target.routingEpoch === fence.routingEpoch
  );
}

/** Re-stamp a target's binding-generation fence with what the operator was
 * shown, without re-deriving any other dimension. */
export function fencedTarget(target: ResourceTarget, fence: LaneFence): ResourceTarget {
  return resourceTarget(target.broker, target.clerkId, {
    accountId: target.accountId,
    entityId: target.entityId,
    capability: target.capability,
    idempotencyKey: target.idempotencyKey,
    bindingGeneration: fence.bindingGeneration,
    routingEpoch: fence.routingEpoch,
  });
}

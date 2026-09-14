import { describe, expect, it } from 'vitest';

import type { LaneDescriptor } from './fleet-directory.types';
import {
  freezeLaneFence,
  laneFenceDrifted,
  laneFenceIsEnforceable,
  laneFenceVerdict,
  LANE_FENCE_CONFLICT_MESSAGE,
  LANE_FENCE_UNENFORCEABLE_MESSAGE,
  type LaneFence,
} from './lane-fence';
import { testLane } from './fleet-directory-testing';

describe('laneFence', () => {
  it('freezes the generation and epoch the operator was shown', () => {
    const fence = freezeLaneFence(testLane({ effective_binding_generation: 3, routing_epoch: 4 }));
    expect(fence).toEqual({ bindingGeneration: 3, routingEpoch: 4 });
    expect(Object.isFrozen(fence)).toBe(true);
  });

  it('reports drift when the lane rebinds under a frozen fence', () => {
    const frozen = freezeLaneFence(testLane({ effective_binding_generation: 3, routing_epoch: 4 }));
    expect(laneFenceDrifted(frozen, testLane({ effective_binding_generation: 3, routing_epoch: 4 }))).toBe(false);
    expect(laneFenceDrifted(frozen, testLane({ effective_binding_generation: 4, routing_epoch: 4 }))).toBe(true);
    expect(laneFenceDrifted(frozen, testLane({ effective_binding_generation: 3, routing_epoch: 5 }))).toBe(true);
  });

  it('treats a lane that vanished from the directory as drift', () => {
    const frozen = freezeLaneFence(testLane({ effective_binding_generation: 3, routing_epoch: 4 }));
    expect(laneFenceDrifted(frozen, undefined)).toBe(true);
  });

  it('does not call an unfenced command frozen', () => {
    // A cold directory yields nulls. The backend fence is `is not None`-gated
    // on both paths, so a null generation dispatches UNFENCED — the caller must
    // be able to tell that apart from a real fence.
    const fence = freezeLaneFence(undefined);
    expect(fence).toEqual({ bindingGeneration: null, routingEpoch: null });
    expect(laneFenceIsEnforceable(fence)).toBe(false);
    expect(laneFenceIsEnforceable({ bindingGeneration: 3, routingEpoch: 4 } as LaneFence)).toBe(true);
  });

  it('refuses an unenforceable fence before it reports drift', () => {
    const frozen = freezeLaneFence(undefined); // bindingGeneration: null
    const warmLane = { effective_binding_generation: 3, routing_epoch: 4 } as LaneDescriptor;

    // Both statements are true of this pair; the unenforceable one is the one shown.
    expect(laneFenceDrifted(frozen, warmLane)).toBe(true);
    expect(laneFenceVerdict(frozen, warmLane)).toEqual({
      ok: false,
      message: LANE_FENCE_UNENFORCEABLE_MESSAGE,
    });
  });

  it('reports drift when the fence was enforceable and the lane moved', () => {
    const frozen = { bindingGeneration: 3, routingEpoch: 4 } as LaneFence;
    const moved = { effective_binding_generation: 4, routing_epoch: 4 } as LaneDescriptor;

    expect(laneFenceVerdict(frozen, moved)).toEqual({
      ok: false,
      message: LANE_FENCE_CONFLICT_MESSAGE,
    });
  });

  it('passes an enforceable fence against an unmoved lane', () => {
    const frozen = { bindingGeneration: 3, routingEpoch: 4 } as LaneFence;
    const same = { effective_binding_generation: 3, routing_epoch: 4 } as LaneDescriptor;

    expect(laneFenceVerdict(frozen, same)).toEqual({ ok: true });
  });
});

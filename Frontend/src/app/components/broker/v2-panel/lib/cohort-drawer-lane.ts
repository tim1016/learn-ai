import { computed, effect, inject, signal, type Signal } from '@angular/core';

import { FleetDirectoryService } from '../../../../fleet/fleet-directory.service';
import {
  freezeLaneFence,
  LANE_FENCE_CONFLICT_MESSAGE,
  laneFenceDrifted,
  laneFenceVerdict,
} from '../../../../fleet/lane-fence';
import { resourceTarget, type ResourceTarget, withCommand } from '../../../../fleet/resource-target';

export interface CohortDrawerLaneInputs {
  readonly visible: () => boolean;
  readonly broker: () => string;
  readonly clerkId: () => string;
  readonly accountId: () => string;
}

/** The lane a cohort drawer reads from and commands against. */
export interface CohortDrawerLane {
  /** The live lane, for the drawer's own presentation read. */
  readonly readTarget: Signal<ResourceTarget>;
  /**
   * The command target frozen when the drawer opened, carrying a durable key
   * minted at that moment. `null` while closed, and `null` when the lane was
   * not enforceable at open — a caller guarded on `null` cannot dispatch.
   */
  readonly presentedTarget: Signal<ResourceTarget | null>;
  /** The lane rebound under the open drawer, or was never fenceable. */
  readonly conflict: Signal<boolean>;
  /** Which of the two conflicts it is — they share one gate, not one sentence. */
  readonly conflictMessage: Signal<string>;
}

/**
 * Freeze a cohort drawer's command lane at open (#2068), shared by every
 * cohort-scoped drawer on the roster (archive, flatten).
 *
 * A lane rebinding while the drawer is open states a conflict rather than
 * re-minting: the operator's typed confirmation must not be silently
 * discarded against a lane they never saw change (#2068, decision 10). Only a
 * reopen (visible false→true) clears it.
 *
 * A cold directory at the moment of open is a distinct refusal, not a drift:
 * `presentedTarget` is deliberately left unset so a caller guarded on `null`
 * cannot dispatch — the operator must reopen once the directory has loaded,
 * not wait for a live rebind that this frozen presentation would never see
 * anyway (#2068, decision 15).
 *
 * Must be called from an injection context: it injects the fleet directory
 * and creates an `effect()`.
 */
export function cohortDrawerLane(inputs: CohortDrawerLaneInputs): CohortDrawerLane {
  const fleetDirectory = inject(FleetDirectoryService);
  const liveLane = computed(() => fleetDirectory.lane(inputs.broker(), inputs.clerkId()));
  const readTarget = computed(() => {
    const fence = freezeLaneFence(liveLane());
    return resourceTarget(inputs.broker(), inputs.clerkId(), {
      accountId: inputs.accountId(),
      bindingGeneration: fence.bindingGeneration,
      routingEpoch: fence.routingEpoch,
    });
  });
  const presentedTarget = signal<ResourceTarget | null>(null);
  const conflict = signal(false);
  const conflictMessage = signal<string>(LANE_FENCE_CONFLICT_MESSAGE);

  let openedTarget: ResourceTarget | null = null;
  effect(() => {
    const visible = inputs.visible();
    const target = readTarget();
    if (!visible) {
      openedTarget = null;
      presentedTarget.set(null);
      conflict.set(false);
      return;
    }
    if (openedTarget === null) {
      openedTarget = target;
      const verdict = laneFenceVerdict(
        { bindingGeneration: target.bindingGeneration, routingEpoch: target.routingEpoch },
        liveLane(),
      );
      if (!verdict.ok) {
        conflict.set(true);
        conflictMessage.set(verdict.message);
        return;
      }
      presentedTarget.set(withCommand(target, 'bot_action', crypto.randomUUID()));
    } else if (
      laneFenceDrifted(
        { bindingGeneration: openedTarget.bindingGeneration, routingEpoch: openedTarget.routingEpoch },
        liveLane(),
      )
    ) {
      conflict.set(true);
      conflictMessage.set(LANE_FENCE_CONFLICT_MESSAGE);
    }
  });

  return {
    readTarget,
    presentedTarget: presentedTarget.asReadonly(),
    conflict: conflict.asReadonly(),
    conflictMessage: conflictMessage.asReadonly(),
  };
}

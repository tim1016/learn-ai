import { computed, effect, inject, resource, signal, untracked, type Signal } from '@angular/core';

import { FleetDirectoryService } from '../../../../fleet/fleet-directory.service';
import {
  freezeLaneFence,
  LANE_FENCE_CONFLICT_MESSAGE,
  laneFenceDrifted,
  laneFenceVerdict,
} from '../../../../fleet/lane-fence';
import { resourceTarget, type ResourceTarget } from '../../../../fleet/resource-target';

export interface CohortDrawerPresentationInputs<V extends { readonly account_id: string }> {
  readonly visible: () => boolean;
  readonly broker: () => string;
  readonly clerkId: () => string;
  readonly accountId: () => string;
  /** The drawer's on-demand presentation read (never polled, ADR 0051 D3). */
  readonly load: (target: ResourceTarget) => Promise<V>;
}

function sameTarget(left: ResourceTarget, right: ResourceTarget): boolean {
  return (
    left.broker === right.broker &&
    left.clerkId === right.clerkId &&
    left.accountId === right.accountId &&
    left.bindingGeneration === right.bindingGeneration &&
    left.routingEpoch === right.routingEpoch
  );
}

/**
 * The presentation read and frozen command lane shared by every cohort-scoped
 * drawer on the roster (archive, flatten).
 *
 * The read target is structurally stable: the fleet directory refreshes in the
 * background and hands back a fresh lane object each time, and a read keyed on
 * object identity would silently turn "fetched on demand, not polled" (ADR
 * 0051 D3) into a re-read on every directory tick — including mid-command.
 *
 * The command lane is frozen at open (#2068). A lane rebinding while the
 * drawer is open states a conflict rather than re-minting: the operator's
 * typed confirmation must not be silently discarded against a lane they never
 * saw change (decision 10). Only a reopen (visible false→true) clears it. A
 * cold directory at open is a distinct refusal: `frozenTarget` stays `null`
 * so nothing can dispatch until the operator reopens (decision 15).
 *
 * Construct in an injection context (a component field initializer): it
 * injects the fleet directory and creates a resource and effects.
 */
export class CohortDrawerPresentation<V extends { readonly account_id: string }> {
  private readonly fleetDirectory = inject(FleetDirectoryService);
  private readonly liveLane = computed(() =>
    this.fleetDirectory.lane(this.inputs.broker(), this.inputs.clerkId()),
  );
  private readonly readTarget = computed(
    () => {
      const fence = freezeLaneFence(this.liveLane());
      return resourceTarget(this.inputs.broker(), this.inputs.clerkId(), {
        accountId: this.inputs.accountId(),
        bindingGeneration: fence.bindingGeneration,
        routingEpoch: fence.routingEpoch,
      });
    },
    { equal: sameTarget },
  );

  private started = 0;
  private readonly presentation = resource({
    params: () => (this.inputs.visible() ? { target: this.readTarget() } : undefined),
    loader: async ({ params }) => {
      // Numbered at START: a read begun before some instant can never pass
      // for one begun after it, however late it resolves.
      const read = ++this.started;
      return { view: await this.inputs.load(params.target), read };
    },
  });
  private readonly current = computed(() => {
    const value = this.presentation.hasValue() ? this.presentation.value() : null;
    // Never hand back another account's legs: the resource keeps its previous
    // value across a params change, and these legs carry act-on-me tokens.
    return value?.view.account_id === this.inputs.accountId() ? value : null;
  });

  /** The latest read for the routed account, or `null` — never another account's. */
  readonly view: Signal<V | null> = computed(() => this.current()?.view ?? null);
  /**
   * The sequence number of the read `view` came from, assigned when that read
   * STARTED. `readsStarted()` sampled at some instant is a watermark: a view
   * whose `read` exceeds it came from a read started after that instant.
   */
  readonly read: Signal<number | null> = computed(() => this.current()?.read ?? null);
  readonly loading: Signal<boolean> = computed(() => this.presentation.isLoading());
  readonly loadFailed: Signal<boolean> = computed(() => this.presentation.error() !== undefined);

  // `resource.reload()` is a no-op while a read is loading. A re-read asked
  // for then is owed after that read settles, not dropped.
  private readonly reloadOwed = signal(false);

  private readonly _frozenTarget = signal<ResourceTarget | null>(null);
  /**
   * The lane target frozen when the drawer opened, fenced but carrying no
   * capability or durable key: each drawer mints its own command identity
   * with `withCommand` under its own key policy. `null` while closed, and
   * `null` when the fence was not enforceable at open — a caller guarded on
   * `null` cannot dispatch.
   */
  readonly frozenTarget: Signal<ResourceTarget | null> = this._frozenTarget.asReadonly();
  private readonly _conflict = signal(false);
  /** The lane rebound under the open drawer, or was never fenceable. */
  readonly conflict: Signal<boolean> = this._conflict.asReadonly();
  private readonly _conflictMessage = signal<string>(LANE_FENCE_CONFLICT_MESSAGE);
  /** Which of the two conflicts it is — they share one gate, not one sentence. */
  readonly conflictMessage: Signal<string> = this._conflictMessage.asReadonly();

  constructor(private readonly inputs: CohortDrawerPresentationInputs<V>) {
    effect(() => {
      if (!this.reloadOwed() || this.loading()) return;
      untracked(() => {
        if (this.presentation.reload()) this.reloadOwed.set(false);
      });
    });

    let openedTarget: ResourceTarget | null = null;
    effect(() => {
      const visible = this.inputs.visible();
      const target = this.readTarget();
      const lane = this.liveLane();
      if (!visible) {
        openedTarget = null;
        this.reloadOwed.set(false);
        this._frozenTarget.set(null);
        this._conflict.set(false);
        return;
      }
      if (openedTarget === null) {
        openedTarget = target;
        const verdict = laneFenceVerdict(
          { bindingGeneration: target.bindingGeneration, routingEpoch: target.routingEpoch },
          lane,
        );
        if (!verdict.ok) {
          this._conflict.set(true);
          this._conflictMessage.set(verdict.message);
          return;
        }
        this._frozenTarget.set(target);
      } else if (
        laneFenceDrifted(
          { bindingGeneration: openedTarget.bindingGeneration, routingEpoch: openedTarget.routingEpoch },
          lane,
        )
      ) {
        this._conflict.set(true);
        this._conflictMessage.set(LANE_FENCE_CONFLICT_MESSAGE);
      }
    });
  }

  /** How many reads have started so far — the watermark source. */
  readsStarted(): number {
    return this.started;
  }

  /** Re-read now, or — if a read is already in flight — once it settles. */
  reload(): void {
    // A closed drawer reads afresh when it next opens; nothing is owed.
    if (!untracked(this.inputs.visible)) return;
    if (!this.presentation.reload()) this.reloadOwed.set(true);
  }
}

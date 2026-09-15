import { Injectable, computed, inject, resource } from '@angular/core';
import { ActivatedRoute } from '@angular/router';
import { toSignal } from '@angular/core/rxjs-interop';

import { BrokersService } from '../../../services/brokers.service';
import { FleetDirectoryService } from '../../../fleet/fleet-directory.service';
import { freezeLaneFence } from '../../../fleet/lane-fence';
import { openLaneFence } from '../../../fleet/open-lane-fence';
import { resourceTarget } from '../../../fleet/resource-target';

/** One account read shared by the desk header and its active lens. */
@Injectable()
export class AlpacaDeskAccountDataService {
  private readonly brokers = inject(BrokersService);
  private readonly fleetDirectory = inject(FleetDirectoryService);
  private readonly route = inject(ActivatedRoute);
  private readonly routeParams = toSignal(this.route.paramMap, {
    initialValue: this.route.snapshot.paramMap,
  });

  /** The rendered route owns lane identity; directory data contributes only
   * the binding/epoch provenance frozen into this resource address. */
  readonly target = computed(() => {
    const clerkId = this.routeParams().get('clerkId');
    const accountId = this.routeParams().get('accountId');
    if (clerkId === null || accountId === null) return null;
    const lane = this.fleetDirectory.lane('alpaca', clerkId);
    if (lane === undefined) return null;
    return resourceTarget('alpaca', clerkId, {
      accountId,
      bindingGeneration: lane.effective_binding_generation ?? null,
      routingEpoch: lane.routing_epoch ?? null,
    });
  });

  /** Route identity only — clerk + account. Passed as `openLaneFence`'s
   * `source`: it is the one thing this desk's command fence should
   * re-derive on. A directory refresh (#2068's mitigating
   * `FleetDirectoryService.refresh()` on a stale-generation refusal, or an
   * unrelated background poll) must not silently re-derive — and therefore
   * un-freeze — the fence below (#2106). */
  private readonly routeIdentity = computed(
    () => `${this.routeParams().get('clerkId') ?? ''}::${this.routeParams().get('accountId') ?? ''}`,
  );

  /** The frozen binding-generation fence for every command-minting surface
   * this desk feeds — order entry, SQLite custody actions, and the
   * transaction-history acknowledgement command. `freeze` is the live
   * directory read; `openLaneFence` wraps it in `untracked()` so it cannot
   * re-derive on its own, and eagerly materializes it as soon as the lane
   * renders (see that helper's doc). `target` above stays live/reactive for
   * reads — only command minting must consume `fence`. */
  readonly fence = openLaneFence(
    () => freezeLaneFence(this.fleetDirectory.lane('alpaca', this.routeParams().get('clerkId') ?? '')),
    () => this.routeIdentity(),
  );

  readonly account = resource({
    params: () => this.target(),
    loader: async ({ params }) => {
      if (params === null) {
        throw new Error('The desk route does not name a rendered Alpaca lane.');
      }
      const account = await this.brokers.getAccount(params);
      if (account.account_id !== params.accountId) {
        throw new Error('The Account Clerk returned an account outside the rendered desk route.');
      }
      return account;
    },
  });
}

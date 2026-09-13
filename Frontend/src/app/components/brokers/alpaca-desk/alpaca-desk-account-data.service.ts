import { Injectable, computed, inject, resource } from '@angular/core';
import { ActivatedRoute } from '@angular/router';
import { toSignal } from '@angular/core/rxjs-interop';

import { BrokersService } from '../../../services/brokers.service';
import { FleetDirectoryService } from '../../../fleet/fleet-directory.service';
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

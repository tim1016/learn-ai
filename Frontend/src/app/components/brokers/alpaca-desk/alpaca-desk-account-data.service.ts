import { Injectable, computed, inject, resource } from '@angular/core';
import { ActivatedRoute } from '@angular/router';
import { toSignal } from '@angular/core/rxjs-interop';

import { BrokersService } from '../../../services/brokers.service';
import { alpacaClerkMatchesAccount, sameAlpacaAccount } from '../../../services/alpaca-account-identity';
import { FleetDirectoryService } from '../../../fleet/fleet-directory.service';
import { laneConfirmedAccount } from '../../../fleet/fleet-directory.types';
import { freezeLaneFence } from '../../../fleet/lane-fence';
import { openLaneFence } from '../../../fleet/open-lane-fence';
import { resourceTarget } from '../../../fleet/resource-target';
import { accountWorkspaceLocation } from '../../../fleet/account-workspace';
import { CurrentUrlService } from '../../../shell/current-url.service';

/** One account read shared by the account workspace's header, its Overview
 * tab's active lens, and the Deploy tab — so the operator's equity, the
 * account the header names, and the account a command is minted against all
 * come from the same confirmed read rather than three of them.
 *
 * `clerkStatus` is the same rule applied to the Clerk↔broker reconciliation
 * read: the workspace's sync indicator and `AlpacaHoldBannerComponent` both
 * name this account's hold and reconciliation state, so both read the one
 * resource here rather than each polling `getClerkStatus` on its own (#2185
 * — the same fact was read three times on one screen). */
@Injectable()
export class AlpacaDeskAccountDataService {
  private readonly brokers = inject(BrokersService);
  private readonly fleetDirectory = inject(FleetDirectoryService);
  private readonly route = inject(ActivatedRoute);
  private readonly routeParams = toSignal(this.route.paramMap, {
    initialValue: this.route.snapshot.paramMap,
  });
  private readonly currentUrl = inject(CurrentUrlService).url;

  /** The account this desk reads.
   *
   * The URL names it on every account-scoped tab, and that is the only answer
   * those tabs ever take. The workspace's lane-scoped tabs — Configuration and
   * the not-ready Bots and Gallery (FR-092) — name no account at all, and for
   * those the lane's own confirmed binding is the account the header is about:
   * "Configuration … renders inside the workspace from the lane's confirmed
   * account" (ADR 0064, FR-092).
   *
   * Read through `accountWorkspaceLocation`, not this service's own
   * `ActivatedRoute`: that route is the one the parent shell is provided on
   * (`brokers/alpaca/clerks/:clerkId`), and `:accountId` belongs to a
   * componentless CHILD segment — inheritance flows parent params down to a
   * child, never a descendant's params up to an ancestor's own injector, so
   * `this.route.paramMap` can never see it. The canonical URL parser is the
   * one reader every workspace surface already shares (`AppComponent`'s
   * title, the menubar's active-node check) and does not depend on where in
   * the route tree it is injected.
   *
   * The route wins wherever it speaks, which is what keeps the directory out
   * of `routeIdentity` below on every URL that mints a command: the fallback
   * is reached only on the lane-scoped tabs, and no command surface renders
   * there. */
  readonly accountId = computed(() => {
    const routed = accountWorkspaceLocation(this.currentUrl())?.accountId ?? null;
    if (routed !== null) return routed;
    const clerkId = this.routeParams().get('clerkId');
    if (clerkId === null) return null;
    const lane = this.fleetDirectory.lane('alpaca', clerkId);
    return lane === undefined ? null : laneConfirmedAccount(lane);
  });

  /** The rendered route owns lane identity; directory data contributes only
   * the binding/epoch provenance frozen into this resource address. */
  readonly target = computed(() => {
    const clerkId = this.routeParams().get('clerkId');
    const accountId = this.accountId();
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
    () => `${this.routeParams().get('clerkId') ?? ''}::${this.accountId() ?? ''}`,
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

  /** `undefined`, not `null`, when there is no account to read: `resource()`
   * skips its loader only for `undefined` params, and a `null` would have run
   * the loader and errored. "No account has been named yet" is not a failed
   * read, and the header says the two differently. */
  readonly account = resource({
    params: () => this.target() ?? undefined,
    loader: async ({ params }) => {
      const account = await this.brokers.getAccount(params);
      if (!sameAlpacaAccount(account.account_id, params.accountId)) {
        throw new Error('The Account Clerk returned an account outside the rendered desk route.');
      }
      return account;
    },
  });

  /** This account's Clerk↔broker reconciliation and hold status, confirmed
   * against the routed account on the same terms as `account`: a Clerk
   * observing another account is a failed read, never a fact rendered under
   * this account's name. */
  readonly clerkStatus = resource({
    params: () => this.target() ?? undefined,
    loader: async ({ params }) => {
      const status = await this.brokers.getClerkStatus(params);
      if (!alpacaClerkMatchesAccount(status, params.accountId ?? '')) {
        throw new Error('The Clerk is observing an account outside the rendered account route.');
      }
      return status;
    },
  });
}

import { Injectable, computed, inject, resource, signal } from '@angular/core';
import { ActivatedRoute } from '@angular/router';
import { toSignal } from '@angular/core/rxjs-interop';

import { BrokersService } from '../../../services/brokers.service';
import { accountMoneyState } from '../../broker/v2-panel/lib/account-money-state';
import { BrokerV2PanelService } from '../../broker/v2-panel/lib/broker-v2-panel.service';
import { alpacaClerkMatchesAccount, sameAlpacaAccount } from '../../../services/alpaca-account-identity';
import { FleetDirectoryService } from '../../../fleet/fleet-directory.service';
import { laneConfirmedAccount } from '../../../fleet/fleet-directory.types';
import { freezeLaneFence } from '../../../fleet/lane-fence';
import { openLaneFence } from '../../../fleet/open-lane-fence';
import { resourceTarget, sameResourceTarget } from '../../../fleet/resource-target';
import { accountWorkspaceLocation } from '../../../fleet/account-workspace';
import { CurrentUrlService } from '../../../shell/current-url.service';

/** One account read shared by the account workspace's header, its Home and
 * the Deploy tab — so the operator's equity, the
 * account the header names, and the account a command is minted against all
 * come from the same confirmed read rather than three of them.
 *
 * `clerkStatus` is the same rule applied to the Clerk↔broker reconciliation
 * read: Activity's sync check names this account's reconciliation state from
 * the one resource here, which the workspace re-reads on its poll, rather
 * than polling `getClerkStatus` on its own (#2185 — the same fact was once
 * read three times on one screen).
 *
 * `money` is the account-money read (PRD #2560 D12): the header's Free to
 * deploy, Cash, Equity and Today, and every money bar on the account's
 * pages, draw from this one resource. */
@Injectable()
export class AlpacaDeskAccountDataService {
  private readonly brokers = inject(BrokersService);
  private readonly panel = inject(BrokerV2PanelService);
  private readonly fleetDirectory = inject(FleetDirectoryService);
  private readonly route = inject(ActivatedRoute);
  private readonly routeParams = toSignal(this.route.paramMap, {
    initialValue: this.route.snapshot.paramMap,
  });
  private readonly currentUrl = inject(CurrentUrlService).url;

  /** The account this desk reads.
   *
   * The URL names it on every account-scoped tab, and that is the only answer
   * those tabs ever take. The workspace's lane-scoped tabs — Settings and a
   * not-ready Home (FR-092) — name no account at all, and for
   * those the lane's own confirmed binding is the account the header is about:
   * Settings stays lane-scoped and renders inside the workspace from the
   * lane's confirmed account (ADR 0064, FR-092).
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
  }, { equal: sameResourceTarget });

  /** Route identity — clerk + account. Alongside explicit operator review,
   * this is the only reason the desk's command fence may re-derive.
   * A directory refresh (#2068's mitigating
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
  private readonly reviewedLane = signal(0);

  readonly fence = openLaneFence(
    () => freezeLaneFence(this.fleetDirectory.lane('alpaca', this.routeParams().get('clerkId') ?? '')),
    () => `${this.routeIdentity()}::${this.reviewedLane()}`,
  );

  /** Explicit operator review opens a new command context; background reads never do. */
  reviewCurrentLane(): void {
    this.reviewedLane.update((revision) => revision + 1);
  }

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

  /** Whether this account's lane declares the bot-panel read the money read
   * is served under — `null` while no lane is resolved. A lane that does not
   * has no money read to fail. */
  private readonly moneyCapable = computed(() => {
    const target = this.target();
    if (target === null) return null;
    return this.fleetDirectory.lane('alpaca', target.clerkId)?.capabilities.includes('bot_panel_read') ?? null;
  });

  /** Where this account's money is, confirmed against the routed account on
   * the same terms as `account`. Render `moneyState`, not this: it is the one
   * projection of this read every money surface shares. */
  readonly money = resource({
    params: () => (this.moneyCapable() ? this.target() ?? undefined : undefined),
    loader: async ({ params }) => {
      const money = await this.panel.getAccountMoney(params);
      if (!sameAlpacaAccount(money.account_id, params.accountId)) {
        throw new Error('The Account Clerk returned money for an account outside the rendered desk route.');
      }
      return money;
    },
  });

  /** This account's money as every surface on its pages renders it — the
   * header's figures, Home's bar, Deploy's Money step, a bot's slice:
   * loading, not served by this lane, unavailable with the backend's reason
   * and next step, or ready (`accountMoneyState`). */
  readonly moneyState = computed(() =>
    accountMoneyState({
      capable: this.moneyCapable(),
      view: this.money.hasValue() ? this.money.value() : undefined,
      error: this.money.error(),
    }),
  );
}

import {
  ChangeDetectionStrategy,
  Component,
  DestroyRef,
  computed,
  inject,
  linkedSignal,
} from '@angular/core';
import { DOCUMENT } from '@angular/common';
import { toSignal } from '@angular/core/rxjs-interop';
import { ActivatedRoute, Router, RouterLink, RouterOutlet } from '@angular/router';

import { AlpacaDeployDrawerComponent } from '../../broker/broker-deploy-page/alpaca-deploy-drawer.component';
import { AlpacaDeskAccountDataService } from '../alpaca-desk/alpaca-desk-account-data.service';
import { AlpacaLaneModeChipComponent } from '../alpaca-desk/alpaca-lane-mode-chip.component';
import { AlpacaAccountSwitcherComponent } from './alpaca-account-switcher.component';
import { LENS_QUERY_PARAM } from '../../broker/shared/lens/lens';
import { fmtCurrency } from '../../broker/format';
import {
  ACCOUNT_WORKSPACE_TABS,
  accountWorkspaceLocation,
  accountWorkspaceTabRoute,
  type AccountWorkspaceLocation,
  type AccountWorkspaceTab,
} from '../../../fleet/account-workspace';
import { FleetDirectoryService } from '../../../fleet/fleet-directory.service';
import { laneIsReady } from '../../../fleet/fleet-directory.types';
import { AlpacaLiveVerdictService, verdictModeChip } from '../../../services/alpaca-live-verdict.service';
import { CurrentUrlService } from '../../../shell/current-url.service';
import { ReceiptLabelPipe } from '../../../shared/pipes/receipt-label.pipe';
import { TimestampDisplayComponent } from '../../../shared/timestamp/timestamp-display.component';

/** How often the header re-reads the account and its Clerk status. Carried
 * over from the Bots roster's own account poll, which this header replaced:
 * equity and the reconciliation verdict both move while an operator sits on
 * one tab, and neither is pushed. */
const ACCOUNT_POLL_MS = 15_000;

/** Why Deploy is unavailable, in the order the operator can act on: no lane
 * to target, then no declared capability, then no confirmed account.
 * Capability evidence is the provider's, never inferred (FR-097). */
const DEPLOY_WITHOUT_LANE = 'This account’s lane has not resolved, so Deploy has no clerk to target.';
const DEPLOY_WITHOUT_CAPABILITY = 'This clerk does not declare Deploy capability.';
const DEPLOY_WITHOUT_ACCOUNT = 'Alpaca has not confirmed this account yet.';

/** Why the Overview tab is not offered on a lane with no confirmed account:
 * it is that account's own page, and there is no account. */
const TAB_WITHOUT_ACCOUNT = 'Opens once Alpaca confirms this lane’s account.';

/** Why a workspace is showing no account's facts: the lane is still coming up,
 * it is up but nothing has bound an account to it, or the directory does not
 * list it at all. `state` is the lane's own backend lifecycle identifier and
 * reaches the operator through `receiptLabel`. */
type WorkspaceAccountStatus =
  | { readonly kind: 'lifecycle'; readonly state: string }
  | { readonly kind: 'unbound' }
  | { readonly kind: 'unresolved' };

/**
 * The account workspace (ADR 0064 Decision 1): one account header over the
 * Overview, Bots, Gallery and Configuration tabs.
 *
 * The operator chooses an account once, in the account list, and the
 * workspace keeps it while they move between its pages — the tabs are
 * children of this route, so switching one does not re-create this shell,
 * its account read, or its per-lane state. The account is carried by the URL
 * alone (FR-091/FR-092); nothing here remembers a last-used account.
 *
 * Every fact on the header is one lane's own: the name from the lane
 * descriptor, the mode from that lane's server-owned live verdict (never
 * composed here — ADR 0011 §7), equity and the reconciliation verdict from
 * this account's own confirmed reads. One lane's failure is its own
 * (FR-093); the header says what it could not read rather than borrowing a
 * sibling's fact.
 *
 * Deploy is a command surface, so it obeys FR-094: the drawer freezes the
 * target it was opened against (see `AlpacaDeployDrawerComponent`), and this
 * header hands it the target of the rendered resource rather than letting it
 * re-read the live directory while the workflow is open.
 */
@Component({
  selector: 'app-alpaca-account-workspace',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [
    AlpacaAccountSwitcherComponent,
    AlpacaDeployDrawerComponent,
    AlpacaLaneModeChipComponent,
    ReceiptLabelPipe,
    RouterLink,
    RouterOutlet,
    TimestampDisplayComponent,
  ],
  templateUrl: './alpaca-account-workspace.component.html',
  styleUrl: './alpaca-account-workspace.component.scss',
  providers: [AlpacaDeskAccountDataService],
})
export class AlpacaAccountWorkspaceComponent {
  private readonly route = inject(ActivatedRoute);
  private readonly router = inject(Router);
  private readonly accountData = inject(AlpacaDeskAccountDataService);
  private readonly fleetDirectory = inject(FleetDirectoryService);
  private readonly liveVerdicts = inject(AlpacaLiveVerdictService);
  private readonly currentUrl = inject(CurrentUrlService).url;
  private readonly document = inject(DOCUMENT);
  private readonly destroyRef = inject(DestroyRef);
  private readonly routeParams = toSignal(this.route.paramMap, {
    initialValue: this.route.snapshot.paramMap,
  });
  private readonly queryParams = toSignal(this.route.queryParamMap, {
    initialValue: this.route.snapshot.queryParamMap,
  });

  protected readonly fmtCurrency = fmtCurrency;

  /** The rendered route owns lane identity — the same stance
   * `AlpacaDeskAccountDataService` takes, so the header and the tab below it
   * can never disagree about which account they serve. The account itself is
   * read from that service rather than re-derived here: on the lane-scoped
   * tabs the URL names none and the lane's confirmed binding is the answer
   * (FR-092), and one resolution is what keeps the header, the tab strip and
   * the account read pointing at the same account. */
  private readonly clerkId = computed(() => this.routeParams().get('clerkId') ?? '');
  private readonly accountId = this.accountData.accountId;

  /** Which tab the URL has open, and the bot's page open under it. Resolved by
   * the shared pure function, the same one the menubar asks whether a URL is
   * inside a workspace at all. */
  private readonly routedLocation = computed(() => accountWorkspaceLocation(this.currentUrl()));

  protected readonly activeTab = computed<AccountWorkspaceTab>(
    () => this.routedLocation()?.tab ?? 'overview',
  );

  protected readonly location = computed<AccountWorkspaceLocation>(() => ({
    broker: 'alpaca',
    clerkId: this.clerkId(),
    accountId: this.accountId(),
    tab: this.activeTab(),
    botSid: this.routedLocation()?.botSid ?? null,
  }));

  /** The four tabs with the route each one links to, or `null` for a tab this
   * workspace has no address for. Built once per location rather than per
   * render, so a tab's `routerLink` is not handed a freshly allocated array on
   * every change-detection pass. */
  protected readonly tabs = computed(() =>
    ACCOUNT_WORKSPACE_TABS.map((tab) => ({
      ...tab,
      route: accountWorkspaceTabRoute(this.location(), tab.id),
    })),
  );

  /** Why a tab has no address here. Overview is the account's own page, so a
   * lane with no confirmed account has none to open — and substituting another
   * lane's is exactly what FR-096 forbids. */
  protected readonly TAB_WITHOUT_ACCOUNT = TAB_WITHOUT_ACCOUNT;

  /** This workspace's lane, or `null` while the directory has not resolved
   * one for the routed clerk — a bad deep link fails in place here (FR-096),
   * it never falls back to another lane. */
  protected readonly lane = computed(
    () => this.fleetDirectory.lane('alpaca', this.clerkId()) ?? null,
  );

  /** The lens perspective the switcher carries to the chosen account. Read
   * from the URL, not from the stored preference: only a perspective the
   * operator addressed is one to keep across a move. */
  protected readonly lens = computed(() => this.queryParams().get(LENS_QUERY_PARAM));

  /** The mode chip, from the same server-owned verdict the shell's account
   * badge renders — including the Shadow authority and, on a live lane, how
   * many instances are armed. */
  protected readonly modeChip = computed(() =>
    verdictModeChip(this.liveVerdicts.stateFor(this.clerkId())),
  );

  protected readonly target = this.accountData.target;

  /** What the header says in place of this account's own facts when the lane
   * has no account to read them from — the Configuration and not-ready tabs of
   * an unbound lane. Equity and a reconciliation verdict belong to an account;
   * a lane without one has a readiness state instead, and saying "$—" and
   * "Not reconciled" would report a failed read where there was no read. */
  protected readonly accountStatus = computed<WorkspaceAccountStatus | null>(() => {
    if (this.accountId() !== null) return null;
    const lane = this.lane();
    if (lane === null) return { kind: 'unresolved' };
    return laneIsReady(lane) ? { kind: 'unbound' } : { kind: 'lifecycle', state: lane.lifecycle_state };
  });

  protected readonly equity = computed(() =>
    this.accountData.account.hasValue() ? this.accountData.account.value().equity : null,
  );

  /** The latest Clerk↔broker reconciliation, or `null` when none has been
   * recorded for this account yet. Read through `AlpacaDeskAccountDataService`
   * — the Overview tab's hold banner names the same fact, so both read the
   * one shared resource rather than each polling `getClerkStatus` on their
   * own (#2185). */
  protected readonly reconciliation = computed(() =>
    this.accountData.clerkStatus.hasValue()
      ? (this.accountData.clerkStatus.value().latest_reconciliation ?? null)
      : null,
  );

  /** What the sync indicator says when there is no verdict to show. The read
   * failing and the account never having been reconciled are different
   * facts, so they read differently. */
  protected readonly syncUnavailable = computed(() =>
    this.accountData.clerkStatus.error() === undefined ? 'Not reconciled' : 'Reconciliation unavailable',
  );

  protected readonly deployBlockedReason = computed(() => {
    const lane = this.lane();
    if (lane === null) return DEPLOY_WITHOUT_LANE;
    if (!lane.capabilities.includes('deploy')) return DEPLOY_WITHOUT_CAPABILITY;
    return this.accountData.account.hasValue() ? null : DEPLOY_WITHOUT_ACCOUNT;
  });

  /** `?deploy` is the deploy entry point's address, not a private flag: the
   * account list's per-account Deploy link, the menubar's Deploy entry and
   * the strategy-validation hand-off all arrive here by navigating to this
   * account with that query param set, and `activeMenuNodeFor` reads the same
   * param to highlight Deploy. So the drawer is seeded from the URL and the
   * two commands below keep the URL saying what is open. */
  protected readonly deployOpen = linkedSignal(() => this.queryParams().has('deploy'));

  constructor() {
    // Equity and the reconciliation verdict both move while the operator
    // stays on one tab. Paused while the tab is hidden, as every other poll
    // on this surface is.
    const accountTimer = setInterval(() => {
      if (this.document.visibilityState !== 'visible') return;
      if (!this.accountData.account.isLoading()) this.accountData.account.reload();
      if (!this.accountData.clerkStatus.isLoading()) this.accountData.clerkStatus.reload();
    }, ACCOUNT_POLL_MS);
    this.destroyRef.onDestroy(() => clearInterval(accountTimer));
  }

  protected openDeploy(): void {
    if (this.deployBlockedReason() !== null) return;
    this.deployOpen.set(true);
    void this.router.navigate([], {
      relativeTo: this.route,
      queryParams: { deploy: '' },
      queryParamsHandling: 'merge',
    });
  }

  protected closeDeploy(): void {
    this.deployOpen.set(false);
    void this.router.navigate([], {
      relativeTo: this.route,
      // `deployLens` is still nulled so an old bookmarked URL cleans itself up.
      queryParams: { deploy: null, deployLens: null },
      queryParamsHandling: 'merge',
    });
  }
}

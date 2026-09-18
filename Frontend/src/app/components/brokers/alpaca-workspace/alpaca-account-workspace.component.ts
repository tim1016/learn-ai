import {
  ChangeDetectionStrategy,
  Component,
  DestroyRef,
  ElementRef,
  computed,
  effect,
  inject,
  viewChild,
} from '@angular/core';
import { DOCUMENT } from '@angular/common';
import { toSignal } from '@angular/core/rxjs-interop';
import { ActivatedRoute, RouterLink, RouterOutlet } from '@angular/router';

import { AlpacaDeskAccountDataService } from '../alpaca-desk/alpaca-desk-account-data.service';
import { AlpacaLaneModeChipComponent } from '../alpaca-desk/alpaca-lane-mode-chip.component';
import { AlpacaAccountSwitcherComponent } from './alpaca-account-switcher.component';
import { BotsPageActionsBridgeService } from './bots-page-actions-bridge.service';
import { LENS_QUERY_PARAM } from '../../../shared/lens/lens';
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
 * Overview, Bots, Gallery, Configuration and Deploy tabs.
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
 * Deploy is one of the five tabs, not an overlay: `AlpacaDeployTabComponent`
 * reads this same `AlpacaDeskAccountDataService` instance for its target
 * (FR-094), so a bind command can never target an account the header itself
 * is not showing.
 */
@Component({
  selector: 'app-alpaca-account-workspace',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [
    AlpacaAccountSwitcherComponent,
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
  private readonly accountData = inject(AlpacaDeskAccountDataService);
  private readonly fleetDirectory = inject(FleetDirectoryService);
  /** The Bots tab's own Refresh/Archive finished commands, while that tab is
   * the one mounted — rendered in the header's action slot below. */
  protected readonly botsPageActions = inject(BotsPageActionsBridgeService).host;
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

  /** Where the URL says we are: which account, which tab, and the bot's page
   * open under it. The only place a workspace URL is parsed here — everything
   * below derives from this one read rather than asking the router again, so
   * no two members of this shell can answer "where am I?" differently.
   * Resolved by the shared pure function, the same one the menubar asks
   * whether a URL is inside a workspace at all. */
  private readonly routedLocation = computed(() => accountWorkspaceLocation(this.currentUrl()));

  /** The routed location plus the one fact the URL cannot carry: on the
   * lane-scoped tabs it names no account and the lane's confirmed binding is
   * the answer (FR-092). */
  protected readonly location = computed<AccountWorkspaceLocation>(() => {
    const routed = this.routedLocation();
    return routed === null
      ? {
          broker: 'alpaca',
          clerkId: this.clerkId(),
          accountId: this.accountId(),
          tab: 'overview',
          botSid: null,
        }
      : { ...routed, accountId: routed.accountId ?? this.accountId() };
  });

  protected readonly activeTab = computed<AccountWorkspaceTab>(() => this.location().tab);

  /** The five tabs with the route each one links to, or `null` for a tab this
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

  private readonly workspaceBody = viewChild.required<ElementRef<HTMLElement>>('workspaceBody');

  /** What the tab body is currently showing: the account, the tab, and the
   * bot's page open under it. A change to any of the three replaces the whole
   * body beneath a header and tab strip that do not move.
   *
   * Keyed on `routedLocation` — parsed from the URL — rather than `location`:
   * `location().accountId` falls back to the lane's confirmed account on the
   * lane-scoped tabs (Configuration, not-ready Bots/Gallery), which resolves
   * asynchronously from the fleet directory. Keying on that async value made
   * this key change on its own, a tick or two after arrival, with no
   * navigation involved — and stole focus out from under the operator when it
   * did. `routedLocation`'s `accountId` is `null` on those tabs by
   * construction (it is parsed straight from the URL), so it only changes
   * when the URL actually does.
   *
   * `null` when the URL is not a workspace URL at all — which no route that
   * renders this shell produces. It is reported rather than papered over with
   * an invented Overview, which would key indistinguishably from a real
   * account-less Overview on the same lane. */
  private readonly renderedContent = computed(() => {
    const routed = this.routedLocation();
    return routed === null
      ? null
      : [routed.clerkId, routed.accountId ?? '', routed.tab, routed.botSid ?? ''].join('::');
  });

  constructor() {
    // Focus follows a tab change and an account switch. The router replaces
    // the body without moving the keyboard, which leaves a keyboard or
    // screen-reader operator standing on the link they just followed while
    // everything below it has changed — so the keyboard is moved into the
    // body, the ARIA tabs practice of landing in the panel that was revealed.
    //
    // The first render is deliberately excluded: arriving on a page must not
    // take focus away from wherever the operator already is.
    let rendered: string | null = null;
    effect(() => {
      const next = this.renderedContent();
      // A URL outside the workspace names no body to land in, and is not a
      // change of body either — it is left out of the record entirely so the
      // tab that was open stays the thing the next key is compared against.
      if (next === null) return;
      const previous = rendered;
      rendered = next;
      if (previous === null || previous === next) return;
      // After this pass has rendered the new body, not during it.
      queueMicrotask(() => this.workspaceBody().nativeElement.focus());
    });

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
}

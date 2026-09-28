import { CurrencyPipe, DOCUMENT } from '@angular/common';
import { ChangeDetectionStrategy, Component, DestroyRef, computed, inject, input, resource } from '@angular/core';
import { RouterLink, type QueryParamsHandling } from '@angular/router';

import { accountWorkspaceEntryRoute, accountWorkspaceTabRoute } from '../../../fleet/account-workspace';
import { FleetDirectoryService } from '../../../fleet/fleet-directory.service';
import {
  type LaneDescriptor,
  laneConfirmedAccount,
  laneDisplayName,
  laneDisplayNameText,
  laneIsReady,
} from '../../../fleet/fleet-directory.types';
import {
  resourceTarget,
  sameResourceTarget,
  type FleetCapability,
  type ResourceTarget,
} from '../../../fleet/resource-target';
import { AlpacaLiveVerdictService, verdictModeChip } from '../../../services/alpaca-live-verdict.service';
import { ReceiptLabelPipe } from '../../../shared/pipes/receipt-label.pipe';
import { MoneyBarComponent } from '../../broker/money-bar/money-bar.component';
import { accountMoneyState } from '../../broker/v2-panel/lib/account-money-state';
import { BrokerV2PanelService } from '../../broker/v2-panel/lib/broker-v2-panel.service';
import { AlpacaLaneModeChipComponent } from './alpaca-lane-mode-chip.component';
import { BrokerConfigurationService } from './configuration/broker-configuration.service';

/** What the card says in place of a fact it could not read. A failed read and
 * a read still in flight are different things and say so — reporting "$—"
 * for either would claim a broker answer that was never given. */
const READINESS_UNAVAILABLE = 'Readiness unavailable';
const READINESS_LOADING = 'Reading readiness…';

/** How often a card re-reads its account's money: the cadence the directory
 * its counts come from is refreshed at (`AlpacaLiveVerdictService` forces one
 * every 30 s), so a card's figures and counts move together. A reload keeps
 * the figures on screen while it runs. */
const CARD_MONEY_POLL_MS = 30_000;

/** What this card can show for its lane, in the same three terms the
 * workspace's own not-ready surfaces use: it is serving an account, its lane
 * is down, or its lane is up with nothing bound to it yet. Exhaustive, so a
 * fourth state is a compile error here rather than a fourth boolean and a
 * fourth `@else`. */
type LaneCardState =
  | { readonly kind: 'serving'; readonly accountId: string }
  | { readonly kind: 'lifecycle'; readonly lifecycleState: string }
  | { readonly kind: 'unbound' };

/**
 * One account's card in the Alpaca account list (ADR 0064 Decision 2).
 *
 * The whole card is a single click target into that account's workspace —
 * its Home, or Settings for a lane with no confirmed account, which
 * is the one tab such a lane can serve and where binding it happens anyway.
 * The destination is the navigation resolver's to decide
 * (`accountWorkspaceEntryRoute`), never composed here, so a card and a shell
 * account badge open the same account in the same place.
 *
 * It carries what the operator chooses an account *by* — its name, its mode
 * worded one way in its lane colour, its money (account money, free to
 * deploy, and a small money bar), how many bots run, hold or dry-run on it,
 * and how many things need the owner — or why it is not ready yet — and none
 * of the lane mechanics (endpoint mode, authority state, binding generation,
 * the raw account id). Those are the workspace's and Settings' facts;
 * a list exists to choose from.
 *
 * Every dollar and width is the account-money read's (PRD #2560 D12); the
 * running, Dry Run and attention counts are one field each of the lane's
 * directory summary, and the stopped-still-holding count is the money read's
 * `stopped_holding_count`. The card adds nothing up. Each read is this card's own
 * `resource()` against this lane's frozen target: one account's failed read
 * is that card's alone and leaves every sibling card whole (FR-093).
 *
 * The host is the account list's list item (`role="listitem"` under the
 * page's `role="list"`), so the list's direct children are its items for
 * layout and for assistive technology alike — this repo's
 * `component-selector` lint rule requires `app-` kebab-case element
 * selectors, so an `<li>` host is not available.
 */
@Component({
  selector: 'app-alpaca-lane-card',
  changeDetection: ChangeDetectionStrategy.OnPush,
  host: { role: 'listitem' },
  imports: [CurrencyPipe, RouterLink, ReceiptLabelPipe, AlpacaLaneModeChipComponent, MoneyBarComponent],
  templateUrl: './alpaca-lane-card.component.html',
  styleUrl: './alpaca-lane-card.component.scss',
})
export class AlpacaLaneCardComponent {
  private readonly liveVerdicts = inject(AlpacaLiveVerdictService);
  private readonly fleetDirectory = inject(FleetDirectoryService);
  private readonly panel = inject(BrokerV2PanelService);
  private readonly configuration = inject(BrokerConfigurationService);
  private readonly document = inject(DOCUMENT);

  readonly lane = input.required<LaneDescriptor>();

  /** A broker-wide deploy intent the list is carrying (`/brokers/alpaca?deploy`
   * — the strategy-validation hand-off's landing URL). The operator asked to
   * deploy before they had an account, so choosing one here is the step that
   * was missing, and the intent travels into that account's own Deploy tab.
   * The list itself offers no Deploy of its own (ADR 0064 Decision 2):
   * deploying is something one does *on* an account. */
  readonly deployIntent = input(false);

  private readonly account = computed(() => laneConfirmedAccount(this.lane()));

  /** It gates what this card *reads*, never where it opens. An account whose
   * lane has gone unreachable is still that account, and the shell's badge
   * and the workspace's switcher both open it at its own workspace — a card
   * that sent the operator somewhere else would make one account two places
   * depending on which affordance they clicked (ADR 0064 Decision 2). */
  protected readonly state = computed<LaneCardState>(() => {
    const lane = this.lane();
    if (!laneIsReady(lane)) return { kind: 'lifecycle', lifecycleState: lane.lifecycle_state };
    const accountId = this.account();
    return accountId === null ? { kind: 'unbound' } : { kind: 'serving', accountId };
  });

  /** This lane's display name: its account nickname, or its lane label
   * until one is set. Siblings come from injecting `FleetDirectoryService`
   * directly (`lanesOf(lane.broker)`), not a prop the parent must remember
   * to pass — one lane card can't render disambiguated from another again. */
  protected readonly displayName = computed(() =>
    laneDisplayName(this.lane(), this.fleetDirectory.lanesOf(this.lane().broker)),
  );

  protected readonly barCaption = computed(() => `Where ${laneDisplayNameText(this.displayName())}’s money is`);

  /** This account's mode chip, from the same server-owned verdict the shell's
   * badges render — never composed on the client (ADR 0011 §7). */
  protected readonly modeChip = computed(() =>
    verdictModeChip(this.liveVerdicts.stateFor(this.lane().clerk_id)),
  );

  /** The lane colour the card is framed in (PRD #2560 D4), or none while the
   * mode is still being read or is unknown. The chip beside it words the
   * mode, so the colour is never the only carrier. */
  protected readonly laneColour = computed(() => {
    const tone = this.modeChip().tone;
    return tone === 'live' || tone === 'paper' || tone === 'shadow' ? tone : null;
  });

  /** The card's one destination: this lane's confirmed account, whatever the
   * lane can currently report about itself. The resolver substitutes
   * Settings only for a lane with no account at all — the same
   * substitution a carried deploy intent falls back to when this lane has no
   * account for Deploy to target either. */
  protected readonly openRoute = computed(() => {
    const lane = this.lane();
    const address = { broker: lane.broker, clerkId: lane.clerk_id, accountId: this.account() };
    if (this.deployIntent()) {
      return accountWorkspaceTabRoute(address, 'deploy') ?? accountWorkspaceEntryRoute(address);
    }
    return accountWorkspaceEntryRoute(address);
  });

  /** Merged only while an intent is being carried: the hand-off arrives as
   * `?deploy=&strategy=…`, and `strategy` is the Deploy tab's own deep link to
   * read — but `deploy` itself is stripped, since Deploy is a path now, not a
   * query param. Without an intent nothing from this URL belongs on the
   * next one. */
  protected readonly openQuery = computed(() => (this.deployIntent() ? { deploy: null } : {}));

  protected readonly openQueryHandling = computed<QueryParamsHandling>(() =>
    this.deployIntent() ? 'merge' : '',
  );

  /** This card's frozen read address, carrying the binding generation and
   * routing epoch observed on the lane it rendered (FR-094/095) — a read-only
   * card still reads under the provenance it was drawn from. `undefined`
   * while there is no account to read, which `resource()` treats as nothing
   * to load rather than as a failed load. */
  private readonly target = computed<ResourceTarget | undefined>(() => {
    const state = this.state();
    if (state.kind !== 'serving') return undefined;
    const lane = this.lane();
    return resourceTarget(lane.broker, lane.clerk_id, {
      accountId: state.accountId,
      bindingGeneration: lane.effective_binding_generation,
      routingEpoch: lane.routing_epoch,
    });
  });

  /** The money read's address, compared by value. A `resource()` re-reads
   * whenever its params function re-runs, even to the same answer, and every
   * directory refresh hands the card a new lane object — so without this the
   * card re-read its money on each refresh. */
  private readonly moneyTarget = computed(() => this.targetFor('bot_panel_read'), { equal: sameResourceTarget });

  protected readonly money = resource({
    params: () => this.moneyTarget(),
    loader: ({ params }) => this.panel.getAccountMoney(params),
  });

  /** The card's account money in the one projection every money surface
   * shares (`accountMoneyState`): the header of the account this card opens
   * words the same state the same way. */
  protected readonly cardMoney = computed(() =>
    accountMoneyState({
      capable: this.lane().capabilities.includes('bot_panel_read'),
      view: this.money.hasValue() ? this.money.value() : undefined,
      error: this.money.error(),
    }),
  );

  /** Running and Dry Run counts, each one field of the lane's directory
   * summary, and the stopped bots still holding money — the money read's own
   * `stopped_holding_count`. A count the backend did not report is said to be
   * unknown, never shown as zero (or left out, as a zero is). */
  protected readonly botCounts = computed<readonly string[]>(() => {
    const summary = this.lane().provider_summary;
    const running = summary?.running_count ?? null;
    const dryRun = summary?.dry_run_count ?? null;
    const money = this.cardMoney();
    const holding = money.kind === 'ready' ? money.view.stopped_holding_count ?? null : null;
    if (running === null || dryRun === null) return ['Bot counts unavailable'];
    return [
      `${running} running`,
      ...(holding === null ? ['Stopped holdings unknown'] : holding > 0 ? [`${holding} stopped, still holding`] : []),
      `${dryRun} Dry Run`,
    ];
  });

  /** How many things need the owner on this account — the lane attention
   * read's own count, carried by the directory. */
  protected readonly attention = computed(() => {
    const count = this.lane().provider_summary?.attention_count ?? null;
    if (count === null) return { needs: false, text: 'Attention unknown' };
    if (count === 0) return { needs: false, text: 'All clear' };
    return { needs: true, text: count === 1 ? '1 needs you' : `${count} need you` };
  });

  /** The unbound lane whose readiness is read, as a string so a directory
   * refresh of the same lane does not re-run the read. */
  private readonly unboundClerkId = computed(() => (this.state().kind === 'unbound' ? this.lane().clerk_id : undefined));

  /** The backend-authored readiness sentence a *ready but unbound* account
   * shows in place of its money and bots. Operator prose the server owns,
   * rendered verbatim — the same `headline` the Settings tab states, so
   * the list and the tab cannot describe one lane's readiness differently.
   *
   * A lane that is *down* is never asked: its clerk is by definition not
   * answering, so the read would fail and the card would report "Readiness
   * unavailable" — a fabricated outage over a known one, the same shape
   * `targetFor` exists to avoid for an undeclared capability (FR-097). The
   * `lifecycle` state is what such a lane says instead. */
  protected readonly readiness = resource({
    params: () => this.unboundClerkId(),
    loader: ({ params }) => this.configuration.readDeskState(params),
  });

  protected readonly readinessLine = computed(() => {
    if (this.readiness.hasValue()) return this.readiness.value().headline;
    return this.readiness.error() === undefined ? READINESS_LOADING : READINESS_UNAVAILABLE;
  });

  /** The ids that name the card's one link by its account and mode, and
   * describe it by everything else it carries — so a screen reader hears
   * "Paper, PAPER · practice money" as the link, not the whole card. */
  protected readonly nameId = computed(() => `lane-card-${this.lane().clerk_id}-name`);
  protected readonly detailId = computed(() => `lane-card-${this.lane().clerk_id}-detail`);

  constructor() {
    // Money moves while the list is open, and nothing pushes it. Paused while
    // the tab is hidden, as every other poll on these pages is.
    const timer = setInterval(() => {
      if (this.document.visibilityState !== 'visible') return;
      if (!this.money.isLoading()) this.money.reload();
    }, CARD_MONEY_POLL_MS);
    inject(DestroyRef).onDestroy(() => clearInterval(timer));
  }

  /** The frozen target, or `undefined` when this lane has not declared the
   * capability the read needs — a capability it never claimed is not a read
   * that failed, and asking anyway would report a refusal as an outage. */
  private targetFor(capability: FleetCapability): ResourceTarget | undefined {
    return this.lane().capabilities.includes(capability) ? this.target() : undefined;
  }
}

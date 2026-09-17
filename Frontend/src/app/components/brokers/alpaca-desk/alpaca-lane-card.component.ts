import { ChangeDetectionStrategy, Component, computed, inject, input, resource } from '@angular/core';
import { RouterLink, type QueryParamsHandling } from '@angular/router';

import { accountWorkspaceEntryRoute } from '../../../fleet/account-workspace';
import { FleetDirectoryService } from '../../../fleet/fleet-directory.service';
import {
  type LaneDescriptor,
  laneConfirmedAccount,
  laneDisplayName,
  laneIsReady,
} from '../../../fleet/fleet-directory.types';
import { resourceTarget, type FleetCapability, type ResourceTarget } from '../../../fleet/resource-target';
import { AlpacaLiveVerdictService, verdictModeChip } from '../../../services/alpaca-live-verdict.service';
import { BrokersService } from '../../../services/brokers.service';
import { fmtCurrency } from '../../broker/format';
import { BrokerV2PanelService } from '../../broker/v2-panel/lib/broker-v2-panel.service';
import { AlpacaLaneModeChipComponent } from './alpaca-lane-mode-chip.component';
import { BrokerConfigurationService } from './configuration/broker-configuration.service';

/** What the card says in place of a fact it could not read. A failed read and
 * a read still in flight are different things and say so — reporting "$—"
 * for either would claim a broker answer that was never given. */
const EQUITY_UNAVAILABLE = 'Equity unavailable';
const EQUITY_LOADING = 'Reading equity…';
const BOTS_UNAVAILABLE = 'Bot count unavailable';
const BOTS_LOADING = 'Reading bots…';
const READINESS_UNAVAILABLE = 'Readiness unavailable';
const READINESS_LOADING = 'Reading readiness…';

/** Why a lane with no capability has no fact rather than a failed read: it
 * declared it cannot serve one, so nothing was ever asked of it (FR-097). */
const EQUITY_WITHOUT_CAPABILITY = 'No account read on this lane';
const BOTS_WITHOUT_CAPABILITY = 'No bot roster on this lane';

/**
 * One account's card in the Alpaca account list (ADR 0064 Decision 2).
 *
 * The whole card is a single click target into that account's workspace —
 * its Overview, or Configuration for a lane with no confirmed account, which
 * is the one tab such a lane can serve and where binding it happens anyway.
 * The destination is the navigation resolver's to decide
 * (`accountWorkspaceEntryRoute`), never composed here, so a card and a shell
 * account badge open the same account in the same place.
 *
 * It carries what the operator chooses an account *by* — its name, its
 * Paper/Live mode, and either the money and bots on it or why it is not ready
 * yet — and none of the lane mechanics (endpoint mode, authority state,
 * binding generation, the raw account id) the card used to list. Those are
 * the workspace's and Configuration's facts; a list exists to choose from.
 *
 * Every fact is this card's own `resource()` read against this lane's frozen
 * target: one account's failed equity, roster or readiness read is that
 * card's alone and leaves every sibling card whole (FR-093).
 *
 * The host renders `display: contents` (no box of its own) so its `<li>`
 * root is the list's effective direct child for layout and the accessibility
 * tree, without an attribute selector — this repo's `component-selector`
 * lint rule requires `app-` kebab-case element selectors.
 */
@Component({
  selector: 'app-alpaca-lane-card',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [RouterLink, AlpacaLaneModeChipComponent],
  templateUrl: './alpaca-lane-card.component.html',
  styleUrl: './alpaca-lane-card.component.scss',
})
export class AlpacaLaneCardComponent {
  private readonly liveVerdicts = inject(AlpacaLiveVerdictService);
  private readonly fleetDirectory = inject(FleetDirectoryService);
  private readonly brokers = inject(BrokersService);
  private readonly panel = inject(BrokerV2PanelService);
  private readonly configuration = inject(BrokerConfigurationService);

  readonly lane = input.required<LaneDescriptor>();

  /** A broker-wide deploy intent the list is carrying (`/brokers/alpaca?deploy`
   * — the strategy-validation hand-off's landing URL). The operator asked to
   * deploy before they had an account, so choosing one here is the step that
   * was missing, and the intent travels into that account's workspace where
   * the drawer opens over it. The list itself offers no Deploy of its own
   * (ADR 0064 Decision 2): deploying is something one does *on* an account. */
  readonly deployIntent = input(false);

  private readonly account = computed(() => laneConfirmedAccount(this.lane()));

  /** An account this list can show money and bots for: its lane is up *and*
   * Alpaca has confirmed the account those facts would belong to. A lane that
   * is up but unbound has no account to read, so it reads as not ready here
   * and opens on Configuration like any other. */
  protected readonly serving = computed(() => laneIsReady(this.lane()) && this.account() !== null);

  /** This lane's display name: its account nickname, or its lane label
   * until one is set. Siblings come from injecting `FleetDirectoryService`
   * directly (`lanesOf(lane.broker)`), not a prop the parent must remember
   * to pass — one lane card can't render disambiguated from another again. */
  protected readonly displayName = computed(() =>
    laneDisplayName(this.lane(), this.fleetDirectory.lanesOf(this.lane().broker)),
  );

  /** This account's mode chip, from the same server-owned verdict the shell's
   * badges render — never composed on the client (ADR 0011 §7). */
  protected readonly modeChip = computed(() =>
    verdictModeChip(this.liveVerdicts.stateFor(this.lane().clerk_id)),
  );

  /** The card's one destination. */
  protected readonly openRoute = computed(() => {
    const lane = this.lane();
    return accountWorkspaceEntryRoute({
      broker: lane.broker,
      clerkId: lane.clerk_id,
      accountId: this.serving() ? this.account() : null,
    });
  });

  protected readonly openQuery = computed(() => (this.deployIntent() ? { deploy: '' } : {}));

  /** Merged only while an intent is being carried: the hand-off arrives as
   * `?deploy=&strategy=…` and the strategy is the drawer's to read, so
   * replacing the query instead of merging it would open an empty drawer.
   * Without an intent nothing from this URL belongs on the next one. */
  protected readonly openQueryHandling = computed<QueryParamsHandling>(() =>
    this.deployIntent() ? 'merge' : '',
  );

  /** This card's frozen read address, carrying the binding generation and
   * routing epoch observed on the lane it rendered (FR-094/095) — a read-only
   * card still reads under the provenance it was drawn from. `undefined`
   * while there is no account to read, which `resource()` treats as nothing
   * to load rather than as a failed load. */
  private readonly target = computed<ResourceTarget | undefined>(() => {
    const lane = this.lane();
    const accountId = this.account();
    if (!this.serving() || accountId === null) return undefined;
    return resourceTarget(lane.broker, lane.clerk_id, {
      accountId,
      bindingGeneration: lane.effective_binding_generation,
      routingEpoch: lane.routing_epoch,
    });
  });

  protected readonly accountSnapshot = resource({
    params: () => this.targetFor('account_read'),
    loader: ({ params }) => this.brokers.getAccount(params),
  });

  protected readonly catalog = resource({
    params: () => this.targetFor('bot_panel_read'),
    loader: ({ params }) => this.panel.getCatalog(params),
  });

  /** The backend-authored readiness sentence a not-ready account shows in
   * place of its money and bots. Operator prose the server owns, rendered
   * verbatim — the same `headline` the Configuration tab states, so the list
   * and the tab cannot describe one lane's readiness differently. */
  protected readonly readiness = resource({
    params: () => (this.serving() ? undefined : this.lane().clerk_id),
    loader: ({ params }) => this.configuration.readDeskState(params),
  });

  protected readonly equityLine = computed(() => {
    if (!this.lane().capabilities.includes('account_read')) return EQUITY_WITHOUT_CAPABILITY;
    if (this.accountSnapshot.hasValue()) return fmtCurrency(this.accountSnapshot.value().equity);
    return this.accountSnapshot.error() === undefined ? EQUITY_LOADING : EQUITY_UNAVAILABLE;
  });

  protected readonly botsLine = computed(() => {
    if (!this.lane().capabilities.includes('bot_panel_read')) return BOTS_WITHOUT_CAPABILITY;
    if (!this.catalog.hasValue()) {
      return this.catalog.error() === undefined ? BOTS_LOADING : BOTS_UNAVAILABLE;
    }
    const running = this.catalog.value().filter((bot) => bot.running).length;
    if (running === 0) return 'No bots running';
    return running === 1 ? '1 bot running' : `${running} bots running`;
  });

  protected readonly readinessLine = computed(() => {
    if (this.readiness.hasValue()) return this.readiness.value().headline;
    return this.readiness.error() === undefined ? READINESS_LOADING : READINESS_UNAVAILABLE;
  });

  /** The frozen target, or `undefined` when this lane has not declared the
   * capability the read needs — a capability it never claimed is not a read
   * that failed, and asking anyway would report a refusal as an outage. */
  private targetFor(capability: FleetCapability): ResourceTarget | undefined {
    return this.lane().capabilities.includes(capability) ? this.target() : undefined;
  }
}

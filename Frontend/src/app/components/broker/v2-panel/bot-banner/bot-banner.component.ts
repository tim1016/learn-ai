import {
  ChangeDetectionStrategy,
  Component,
  computed,
  inject,
  input,
  output,
} from '@angular/core';
import { RouterLink } from '@angular/router';
import { accountWorkspaceDeployAgainRoute } from '../../../../fleet/account-workspace';

import { AlpacaLaneModeChipComponent } from '../../../brokers/alpaca-desk/alpaca-lane-mode-chip.component';
import {
  AlpacaLiveVerdictService,
  verdictModeChip,
  type LaneModeChip,
} from '../../../../services/alpaca-live-verdict.service';
import { AssetIdentityComponent } from '../../../../shared/asset-identity';
import { TimestampDisplayComponent } from '../../../../shared/timestamp/timestamp-display.component';
import { buildManualOrderTicketNavigation } from '../../lib/manual-order-navigation';
import type {
  BotPanelView,
  CurrentRunState,
  PanelActionTrigger,
} from '../lib/broker-v2-panel.types';
import { PanelActionButtonComponent } from '../panel-action-button/panel-action-button.component';
import { MissionVerdictStatusComponent } from '../bot-detail-banner/mission-verdict-status.component';
import { BotBannerOverflowComponent } from '../bot-detail-banner/bot-banner-overflow.component';
import { BotBannerRunTimingComponent } from './bot-banner-run-timing.component';
import { actionTone, primaryAction } from '../bot-detail-banner/lifecycle-action';

/**
 * The bot page's header (PRD #2560 D2): which bot this is, the way back, its
 * world, state, strategy and symbol, its run timing, and one primary action.
 *
 * WHICH action is primary is the backend's (`BotPanelView.primary_action`):
 * a recovery cure, Stop for a running bot, or none for a stopped one. A
 * stopped bot also offers Deploy again, which starts a new bot and never
 * takes over what this one still holds. The More menu carries the manual
 * order ticket. It never offers Archive, although the backend still presents
 * it: clearing a finished bot is Home's Finished fold alone (owner decision
 * 2026-09-28).
 *
 * A Dry Run bot is marked as simulated cash, never with the lane's colour
 * (hurdle H23): its money is not the account's.
 */
@Component({
  selector: 'app-bot-banner',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [
    AlpacaLaneModeChipComponent,
    AssetIdentityComponent,
    TimestampDisplayComponent,
    RouterLink,
    PanelActionButtonComponent,
    MissionVerdictStatusComponent,
    BotBannerOverflowComponent,
    BotBannerRunTimingComponent,
  ],
  templateUrl: './bot-banner.component.html',
  styleUrl: './bot-banner.component.scss',
})
export class BotBannerComponent {
  readonly panel = input.required<BotPanelView>();
  readonly runState = input.required<CurrentRunState>();
  readonly backRoute = input.required<readonly string[]>();
  readonly backLabel = input.required<string>();
  readonly clerkId = input.required<string>();
  /** The account as this workspace routes it; every link the header builds uses it. */
  readonly routeAccountId = input.required<string>();
  readonly actionPending = input(false);

  readonly actionRequested = output<PanelActionTrigger>();
  readonly retryRequested = output();

  private readonly liveVerdicts = inject(AlpacaLiveVerdictService);

  protected readonly dryRun = computed(() => this.panel().mode === 'dry_run');

  /** The lane's world, worded once, for a bot that trades the lane's money. */
  protected readonly worldChip = computed<LaneModeChip | null>(() =>
    this.dryRun() ? null : verdictModeChip(this.liveVerdicts.stateFor(this.clerkId())),
  );

  /** Deploy again for this bot, under the routed account. */
  protected readonly deployAgain = computed(() => accountWorkspaceDeployAgainRoute({
    broker: this.panel().broker,
    clerkId: this.clerkId(),
    accountId: this.routeAccountId(),
  }, this.panel().strategy_instance_id));

  protected readonly primaryAction = computed(() => primaryAction(this.panel()));
  protected readonly primaryActionTone = computed(() => actionTone(this.primaryAction()));

  /** Screen-reader summary of the latest panel revision, without visual header noise. */
  protected readonly snapshotStatus = computed(
    () => `Revision ${this.panel().revision}${this.panel().health.running ? ' running' : ' stopped'}`,
  );

  /** A Dry Run bot trades no account money, so it has no manual ticket to open. */
  protected readonly manualOrderNavigation = computed(() => this.dryRun() ? null
    : buildManualOrderTicketNavigation({
      broker: this.panel().broker,
      clerkId: this.clerkId(),
      routeAccountId: this.routeAccountId(),
      accountId: this.panel().account_id,
      symbol: this.panel().symbol,
    }));
}

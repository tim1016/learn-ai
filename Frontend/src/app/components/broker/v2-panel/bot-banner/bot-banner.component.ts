import {
  ChangeDetectionStrategy,
  Component,
  computed,
  input,
  output,
} from '@angular/core';
import { RouterLink } from '@angular/router';

import { AssetIdentityComponent } from '../../../../shared/asset-identity';
import { TimestampDisplayComponent } from '../../../../shared/timestamp/timestamp-display.component';
import type { TickerQuoteView } from '../../../../shared/ticker-quote/ticker-quote.component';
import type { DeskLens } from '../../../../shared/lens/lens';
import { buildManualOrderTicketNavigation } from '../../lib/manual-order-navigation';
import type {
  ActionId,
  BotPanelView,
  CurrentRunState,
  PanelAction,
  PanelActionTrigger,
} from '../lib/broker-v2-panel.types';
import { PanelActionButtonComponent } from '../panel-action-button/panel-action-button.component';
import { MissionVerdictStatusComponent } from '../bot-detail-banner/mission-verdict-status.component';
import { BotBannerOverflowComponent } from '../bot-detail-banner/bot-banner-overflow.component';
import { PanelInstrumentQuoteComponent } from '../instrument-quote/panel-instrument-quote.component';
import { BotBannerRunTimingComponent } from './bot-banner-run-timing.component';
import {
  actionTone,
  primaryActionForLens,
} from '../bot-detail-banner/lifecycle-action';

/**
 * The safe-flatten two-step, most-advanced first. `execute_safe_flatten` is
 * only presented once a reducing-order plan exists, so when the backend
 * offers both, the execute step is the one the operator actually wants.
 */
const SAFE_FLATTEN_ACTION_IDS: readonly ActionId[] = [
  'execute_safe_flatten',
  'prepare_safe_flatten',
];

const OVERFLOW_ACTION_IDS: readonly ActionId[] = [
  'retire',
  'archive',
  ...SAFE_FLATTEN_ACTION_IDS,
];

/**
 * The one bot-detail banner (ADR 0064 Decision 1 + the Trader/Operator
 * lifecycle-action banners it replaces): which bot this is, the way back,
 * its latest run's Started/Ended times, and its live status/actions, all in
 * one card. Both lenses render the same instance; only `lens` differs which
 * extra content (a live quote, the promoted safe-flatten action, and the
 * richer overflow) appears. The Trader/Operator switch itself lives in the
 * global top bar (`ActiveLensBridgeService`), not here.
 */
@Component({
  selector: 'app-bot-banner',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [
    AssetIdentityComponent,
    TimestampDisplayComponent,
    RouterLink,
    PanelActionButtonComponent,
    MissionVerdictStatusComponent,
    BotBannerOverflowComponent,
    PanelInstrumentQuoteComponent,
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
  readonly tickerQuote = input<TickerQuoteView | null>(null);
  readonly actionPending = input(false);
  readonly lens = input.required<DeskLens>();

  readonly actionRequested = output<PanelActionTrigger>();
  readonly retryRequested = output();

  protected readonly operator = computed(() => this.lens() === 'operator');

  protected readonly primaryAction = computed(() =>
    primaryActionForLens(this.panel(), this.operator() ? 'operator' : 'trader'),
  );
  protected readonly primaryActionTone = computed(() => actionTone(this.primaryAction()));

  /** Screen-reader summary of the latest panel revision, without visual header noise. */
  protected readonly snapshotStatus = computed(
    () => `Revision ${this.panel().revision}${this.panel().health.running ? ' running' : ' stopped'}`,
  );

  protected readonly manualOrderNavigation = computed(() =>
    buildManualOrderTicketNavigation(
      this.panel().broker,
      this.clerkId(),
      this.panel().account_id,
      this.panel().symbol,
    ),
  );

  /**
   * Exposure stranded by a stop is the state that most needs flatten (#1778,
   * S6). Operator-only: a running bot's exposure belongs to the strategy,
   * and the banner must not invite the trader to fight it, and the trader
   * lens has no flatten action to promote in the first place.
   */
  protected readonly promotedFlattenAction = computed<PanelAction | null>(() => {
    if (!this.operator()) return null;
    const panel = this.panel();
    if (panel.health.running) return null;
    if (!Object.values(panel.exposure).some((quantity) => quantity !== 0)) return null;
    for (const actionId of SAFE_FLATTEN_ACTION_IDS) {
      const action = panel.actions.find((item) => item.action_id === actionId);
      if (action) return action;
    }
    return null;
  });

  protected readonly promotedFlattenTone = computed(() =>
    actionTone(this.promotedFlattenAction()),
  );

  /** A promoted action is already first-class; listing it twice is noise. */
  protected readonly overflowActions = computed(() => {
    if (!this.operator()) return [];
    const promotedId = this.promotedFlattenAction()?.action_id ?? null;
    return this.panel().actions.filter(
      (action) =>
        OVERFLOW_ACTION_IDS.includes(action.action_id) && action.action_id !== promotedId,
    );
  });
}

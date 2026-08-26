import {
  ChangeDetectionStrategy,
  Component,
  computed,
  input,
  output,
} from '@angular/core';

import type {
  ActionId,
  BotPanelView,
  PanelAction,
  PanelActionTrigger,
} from '../../lib/broker-v2-panel.types';
import { ReceiptLabelPipe } from '../../../../../shared/pipes/receipt-label.pipe';
import type { TickerQuoteView } from '../../../../../shared/ticker-quote/ticker-quote.component';
import { PanelActionButtonComponent } from '../../panel-action-button/panel-action-button.component';
import { BotDetailBannerComponent } from '../../bot-detail-banner/bot-detail-banner.component';
import { MissionVerdictStatusComponent } from '../../bot-detail-banner/mission-verdict-status.component';
import { BotBannerOverflowComponent } from '../../bot-detail-banner/bot-banner-overflow.component';
import { PanelInstrumentQuoteComponent } from '../../instrument-quote/panel-instrument-quote.component';
import {
  actionTone,
  primaryActionForLens,
} from '../../bot-detail-banner/lifecycle-action';

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
  'rebuild_from_mirror',
  'reset_authority',
  ...SAFE_FLATTEN_ACTION_IDS,
];

/** The operator's rich but compact bot-detail banner. */
@Component({
  selector: 'app-operator-bot-banner',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [
    BotDetailBannerComponent,
    BotBannerOverflowComponent,
    MissionVerdictStatusComponent,
    PanelActionButtonComponent,
    PanelInstrumentQuoteComponent,
    ReceiptLabelPipe,
  ],
  templateUrl: './operator-bot-banner.component.html',
  styleUrl: './operator-bot-banner.component.scss',
})
export class OperatorBotBannerComponent {
  readonly panel = input.required<BotPanelView>();
  readonly tickerQuote = input<TickerQuoteView | null>(null);
  readonly actionPending = input(false);
  readonly actionRequested = output<PanelActionTrigger>();

  protected readonly backLink = computed(() => [
    '/brokers',
    this.panel().broker,
    'accounts',
    this.panel().account_id,
    'bots',
  ]);
  protected readonly primaryAction = computed(() => primaryActionForLens(this.panel(), 'operator'));
  protected readonly primaryActionTone = computed(() => actionTone(this.primaryAction()));

  /**
   * Exposure stranded by a stop is the state that most needs flatten, and it
   * was the state where flatten was hardest to reach — folded into the
   * overflow, or one collapsed readiness accordion deep (#1778, S6). A
   * stopped bot still holding attributed exposure promotes it beside Resume.
   *
   * Only while stopped: a running bot's exposure belongs to the strategy,
   * and the banner must not invite the operator to fight it.
   */
  protected readonly promotedFlattenAction = computed<PanelAction | null>(() => {
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
    const promotedId = this.promotedFlattenAction()?.action_id ?? null;
    return this.panel().actions.filter(
      (action) =>
        OVERFLOW_ACTION_IDS.includes(action.action_id) && action.action_id !== promotedId,
    );
  });

}

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
  lifecycleActionTone,
  primaryLifecycleAction,
} from '../../bot-detail-banner/lifecycle-action';

const OVERFLOW_ACTION_IDS: readonly ActionId[] = [
  'retire',
  'rebuild_from_mirror',
  'reset_authority',
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
  protected readonly primaryAction = computed(() => primaryLifecycleAction(this.panel()));
  protected readonly primaryActionTone = computed(() => {
    const action = this.primaryAction();
    return action === null ? 'primary' : lifecycleActionTone(action);
  });
  protected readonly overflowActions = computed(() =>
    this.panel().actions.filter((action) => OVERFLOW_ACTION_IDS.includes(action.action_id)),
  );

}

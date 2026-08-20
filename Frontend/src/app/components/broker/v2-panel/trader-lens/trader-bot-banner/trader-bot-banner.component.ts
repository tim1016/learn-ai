import {
  ChangeDetectionStrategy,
  Component,
  computed,
  input,
  output,
} from '@angular/core';
import { RouterLink } from '@angular/router';

import type {
  BotPanelView,
  PanelActionTrigger,
} from '../../lib/broker-v2-panel.types';
import { ReceiptLabelPipe } from '../../../../../shared/pipes/receipt-label.pipe';
import { buildManualOrderTicketNavigation } from '../../../lib/manual-order-navigation';
import { PanelActionButtonComponent } from '../../panel-action-button/panel-action-button.component';
import { BotDetailBannerComponent } from '../../bot-detail-banner/bot-detail-banner.component';
import { MissionVerdictStatusComponent } from '../../bot-detail-banner/mission-verdict-status.component';
import { BotBannerOverflowComponent } from '../../bot-detail-banner/bot-banner-overflow.component';
import {
  actionTone,
  primaryActionForLens,
} from '../../bot-detail-banner/lifecycle-action';

/** The trader's concise, action-oriented bot-detail banner. */
@Component({
  selector: 'app-trader-bot-banner',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [
    BotDetailBannerComponent,
    BotBannerOverflowComponent,
    MissionVerdictStatusComponent,
    PanelActionButtonComponent,
    ReceiptLabelPipe,
    RouterLink,
  ],
  templateUrl: './trader-bot-banner.component.html',
  styleUrl: './trader-bot-banner.component.scss',
})
export class TraderBotBannerComponent {
  readonly panel = input.required<BotPanelView>();
  readonly actionPending = input(false);
  readonly actionRequested = output<PanelActionTrigger>();

  protected readonly backLink = computed(() => [
    '/brokers',
    this.panel().broker,
    'accounts',
    this.panel().account_id,
    'bots',
  ]);
  protected readonly primaryAction = computed(() => primaryActionForLens(this.panel(), 'trader'));
  protected readonly primaryActionTone = computed(() => actionTone(this.primaryAction()));
  protected readonly manualOrderNavigation = computed(() =>
    buildManualOrderTicketNavigation(
      this.panel().broker,
      this.panel().account_id,
      this.panel().symbol,
    ),
  );

}

import {
  ChangeDetectionStrategy,
  Component,
  computed,
  input,
  output,
  signal,
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
import {
  lifecycleActionTone,
  primaryLifecycleAction,
} from '../../bot-detail-banner/lifecycle-action';

/** The trader's concise, action-oriented bot-detail banner. */
@Component({
  selector: 'app-trader-bot-banner',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [
    BotDetailBannerComponent,
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

  protected readonly overflowOpen = signal(false);

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
  protected readonly manualOrderNavigation = computed(() =>
    buildManualOrderTicketNavigation(
      this.panel().broker,
      this.panel().account_id,
      this.panel().symbol,
    ),
  );

  protected toggleOverflow(): void {
    this.overflowOpen.update((open) => !open);
  }
}

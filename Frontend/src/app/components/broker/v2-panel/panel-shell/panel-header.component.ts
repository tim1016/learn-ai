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
  PanelAction,
  PanelActionTrigger,
} from '../lib/broker-v2-panel.types';
import {
  TickerQuoteComponent,
  type TickerQuoteView,
} from '../../../../shared/ticker-quote/ticker-quote.component';
import { ReceiptLabelPipe } from '../../../../shared/pipes/receipt-label.pipe';
import { TimestampDisplayComponent } from '../../../../shared/timestamp/timestamp-display.component';
import { AssetIdentityComponent } from '../../../../shared/asset-identity';
import { PanelActionButtonComponent } from '../panel-action-button/panel-action-button.component';
import { buildManualOrderTicketNavigation } from '../../lib/manual-order-navigation';
import {
  lifecycleActionTone,
  primaryLifecycleAction,
} from '../bot-detail-banner/lifecycle-action';

@Component({
  selector: 'app-panel-header',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [
    PanelActionButtonComponent,
    ReceiptLabelPipe,
    RouterLink,
    TimestampDisplayComponent,
    TickerQuoteComponent,
    AssetIdentityComponent,
  ],
  templateUrl: './panel-header.component.html',
  styleUrl: './panel-header.component.scss',
})
export class PanelHeaderComponent {
  readonly panel = input.required<BotPanelView>();
  readonly tickerQuote = input<TickerQuoteView | null>(null);
  readonly actionPending = input(false);
  readonly actionRequested = output<PanelActionTrigger>();

  protected readonly manualOrderNavigation = computed(() =>
    buildManualOrderTicketNavigation(
      this.panel().broker,
      this.panel().account_id,
      this.panel().symbol,
    ),
  );

  protected readonly botHeadline = computed(() => {
    const health = this.panel().health;
    return health.duty_outcome?.explanation ?? health.desired_state_label;
  });

  protected readonly primaryAction = computed<PanelAction | null>(() => primaryLifecycleAction(this.panel()));

  protected readonly primaryActionTone = computed(() => {
    const action = this.primaryAction();
    return action === null ? 'primary' : lifecycleActionTone(action);
  });

  protected formatAge(ageMs: number | null): string {
    if (ageMs === null) return 'No bar received';
    if (ageMs < 60_000) return `${Math.floor(ageMs / 1_000)}s old`;
    return `${Math.floor(ageMs / 60_000)}m old`;
  }

}

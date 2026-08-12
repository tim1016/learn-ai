import {
  ChangeDetectionStrategy,
  Component,
  computed,
  inject,
  input,
  output,
  resource,
} from '@angular/core';
import { RouterLink } from '@angular/router';
import { firstValueFrom } from 'rxjs';
import type {
  ActionId,
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
import { MarketDataService } from '../../../../services/market-data.service';
import { buildManualOrderTicketNavigation } from '../../lib/manual-order-navigation';

const RUNNING_STOP_ACTION_IDS: readonly ActionId[] = [
  'stop',
  'stop_bot_decisions',
];

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
  private readonly marketData = inject(MarketDataService);

  readonly panel = input.required<BotPanelView>();
  readonly actionPending = input(false);
  readonly actionRequested = output<PanelActionTrigger>();

  private readonly marketSnapshot = resource({
    params: () => this.panel().symbol,
    loader: ({ params: symbol }) =>
      firstValueFrom(this.marketData.getStockSnapshot(symbol)),
  });

  protected readonly tickerQuote = computed<TickerQuoteView | null>(() => {
    const snapshot = this.marketSnapshot.hasValue()
      ? this.marketSnapshot.value().snapshot
      : null;
    const price = snapshot?.day?.close ?? snapshot?.min?.close;
    const changePercent = snapshot?.todaysChangePercent;
    if (
      price === null
      || price === undefined
      || changePercent === null
      || changePercent === undefined
    ) {
      return null;
    }
    return {
      ticker: snapshot?.ticker ?? this.panel().symbol,
      price,
      change: snapshot?.todaysChange,
      changePercent,
    };
  });

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

  protected readonly primaryAction = computed<PanelAction | null>(() => {
    const health = this.panel().health;
    const actionIds: readonly ActionId[] = !health.running
      ? ['resume']
      : health.desired_state === 'PAUSED'
        ? ['continue']
        : RUNNING_STOP_ACTION_IDS;
    return this.panel().actions.find((action) => actionIds.includes(action.action_id)) ?? null;
  });

  protected readonly primaryActionTone = computed(() => {
    const action = this.primaryAction();
    return action !== null && RUNNING_STOP_ACTION_IDS.includes(action.action_id)
      ? 'danger'
      : 'primary';
  });

  protected formatAge(ageMs: number | null): string {
    if (ageMs === null) return 'No bar received';
    if (ageMs < 60_000) return `${Math.floor(ageMs / 1_000)}s old`;
    return `${Math.floor(ageMs / 60_000)}m old`;
  }

}

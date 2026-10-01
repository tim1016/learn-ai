import { ChangeDetectionStrategy, Component, input } from '@angular/core';
import type {
  ClerkCard,
  FeedContinuityView,
  MarketPulseView,
} from '../lib/broker-v2-panel.types';
import { TimestampDisplayComponent } from '../../../../shared/timestamp/timestamp-display.component';
import { ChannelHealthDotComponent } from '../channel-health-dot/channel-health-dot.component';

/**
 * The bot page's Connections fold body (#2563): the market-data and order
 * channels, the current run's IBKR feed continuity, the market's state, and
 * the account facts that gate trading (hold, freeze, reconciliation, orders
 * in doubt).
 *
 * Read-only. The commands that cure these facts (Reconcile, Clear hold) live
 * in the Checks fold beside their gates. The account number is deliberately
 * absent (ADR 0064: it appears only on Configuration and in a consequential
 * action's confirmation); a Dry Run bot's page once showed the real Live
 * account here instead of its simulated one.
 */
@Component({
  selector: 'app-bot-connections',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [ChannelHealthDotComponent, TimestampDisplayComponent],
  templateUrl: './bot-connections.component.html',
  styleUrl: './bot-connections.component.scss',
})
export class BotConnectionsComponent {
  readonly clerk = input.required<ClerkCard>();
  readonly feedContinuity = input.required<FeedContinuityView>();
  readonly marketPulse = input.required<MarketPulseView>();
}

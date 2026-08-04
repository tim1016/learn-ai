import {
  ChangeDetectionStrategy,
  Component,
  input,
  output,
} from '@angular/core';
import type {
  ChannelState,
  ClerkCard,
  PanelAction,
  PanelActionTrigger,
} from '../lib/broker-v2-panel.types';
import { TimestampDisplayComponent } from '../../../../shared/timestamp/timestamp-display.component';
import { BrokerV2CardHelpButtonComponent } from '../help-drawer/broker-v2-card-help-button.component';
import { PanelActionButtonComponent } from '../panel-action-button/panel-action-button.component';

/**
 * Account/clerk card (spec §7.3).
 *
 * Hold state, reconciliation verdict + last sweep, outstanding uncertain
 * intents, channel health.
 *
 * Actions: Reconcile now and Clear hold.
 * - Clear hold: enabled only when root condition is healthy AND freshly observed;
 *   confirmation shows the account-wide blast radius (§7.3).
 * - No force-override path (spec decision #20).
 *
 */
@Component({
  selector: 'app-clerk-card',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [
    BrokerV2CardHelpButtonComponent,
    PanelActionButtonComponent,
    TimestampDisplayComponent,
  ],
  templateUrl: './clerk-card.component.html',
  styleUrl: './clerk-card.component.scss',
})
export class ClerkCardComponent {
  readonly clerk = input.required<ClerkCard>();
  readonly reconcileAction = input<PanelAction | null>(null);
  readonly clearHoldAction = input<PanelAction | null>(null);
  readonly actionPending = input(false);

  readonly actionRequested = output<PanelActionTrigger>();

  protected readonly channelIcon: Record<ChannelState, string> = {
    healthy: '●',
    unhealthy: '✕',
    unknown: '?',
  };

  protected trackChannel(_index: number, channel: { stream: string }): string {
    return channel.stream;
  }
}

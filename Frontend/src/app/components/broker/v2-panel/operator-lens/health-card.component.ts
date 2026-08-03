import {
  ChangeDetectionStrategy,
  Component,
  input,
  output,
} from '@angular/core';
import { KeyValuePipe } from '@angular/common';
import type {
  BotHealthCard,
  PanelAction,
  PanelActionTrigger,
} from '../lib/broker-v2-panel.types';
import { TimestampDisplayComponent } from '../../../../shared/timestamp/timestamp-display.component';
import { BrokerV2CardHelpButtonComponent } from '../help-drawer/broker-v2-card-help-button.component';
import { PanelActionButtonComponent } from '../panel-action-button/panel-action-button.component';

/**
 * Bot health card (spec §7.2).
 *
 * Phase, desired state, duty outcome (kind + backend reason), decision-receipt
 * freshness, last bar seen, and the terminal Retire action.
 *
 */
@Component({
  selector: 'app-health-card',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [
    BrokerV2CardHelpButtonComponent,
    KeyValuePipe,
    PanelActionButtonComponent,
    TimestampDisplayComponent,
  ],
  templateUrl: './health-card.component.html',
  styleUrl: './health-card.component.scss',
})
export class HealthCardComponent {
  readonly health = input.required<BotHealthCard>();
  /** The retire action from the presented-actions list, or null if not presented. */
  readonly retireAction = input<PanelAction | null>(null);
  readonly actionPending = input(false);

  readonly actionRequested = output<PanelActionTrigger>();
}

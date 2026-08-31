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
  /**
   * The presented registration-exit actions: `retire` for a provably dead
   * registration (#1795), `archive` for one the operator is finished with
   * (ADR 0052). Both are irreversible and both are presented only when the
   * backend has armed them, so the card renders whatever it is handed rather
   * than knowing which exits exist.
   */
  readonly exitActions = input<readonly PanelAction[]>([]);
  readonly actionPending = input(false);

  readonly actionRequested = output<PanelActionTrigger>();
}

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
  StartupJoinView,
} from '../lib/broker-v2-panel.types';
import { TimestampDisplayComponent } from '../../../../shared/timestamp/timestamp-display.component';
import { PanelActionButtonComponent } from '../panel-action-button/panel-action-button.component';
import { ExposureNoticesComponent } from '../startup-join/exposure-notices.component';
import { StartupJoinStatusComponent } from '../startup-join/startup-join-status.component';

/**
 * Bot health card (spec §7.2).
 *
 * Phase, desired state, the run's startup preparation (#2410), duty outcome
 * (kind + backend reason, and what a startup refusal left at the broker), and
 * the terminal Retire action. Activity clocks are promoted into the shared run-timing strip.
 *
 */
@Component({
  selector: 'app-health-card',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [
    ExposureNoticesComponent,
    KeyValuePipe,
    PanelActionButtonComponent,
    StartupJoinStatusComponent,
    TimestampDisplayComponent,
  ],
  templateUrl: './health-card.component.html',
  styleUrl: './health-card.component.scss',
})
export class HealthCardComponent {
  readonly health = input.required<BotHealthCard>();
  /** Where the current run is in joining warmup to its live stream (#2410). */
  readonly startupJoin = input<StartupJoinView | null>(null);
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

import {
  ChangeDetectionStrategy,
  Component,
  input,
} from '@angular/core';
import type {
  BotHealthCard,
  StartupJoinView,
} from '../lib/broker-v2-panel.types';
import { TimestampDisplayComponent } from '../../../../shared/timestamp/timestamp-display.component';
import { ExposureNoticesComponent } from '../startup-join/exposure-notices.component';
import { StartupJoinStatusComponent } from '../startup-join/startup-join-status.component';

/**
 * Bot health card (spec §7.2).
 *
 * Phase, desired state, the run's startup preparation (#2410), duty outcome
 * (kind + backend reason, and what a startup refusal left at the broker).
 * Activity clocks are promoted into the shared run-timing strip.
 *
 */
@Component({
  selector: 'app-health-card',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [
    ExposureNoticesComponent,
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
}

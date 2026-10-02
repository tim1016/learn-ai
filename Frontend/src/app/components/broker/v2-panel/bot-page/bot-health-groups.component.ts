import { ChangeDetectionStrategy, Component, computed, input } from '@angular/core';

import { TimestampDisplayComponent } from '../../../../shared/timestamp/timestamp-display.component';
import type {
  BotHealthCard,
  BotHealthGroupsView,
  HealthLineView,
  MarketPulseView,
  StartupJoinView,
} from '../lib/broker-v2-panel.types';
import { ExposureNoticesComponent } from '../startup-join/exposure-notices.component';
import { StartupJoinStatusComponent } from '../startup-join/startup-join-status.component';

/**
 * The bot's health in two groups (#2794 R9): what happened during this run,
 * and the account right now, then the market for its symbol. Each line is
 * the backend's -- its state, value and note -- so an account condition reads
 * as the account's, and a stopped bot's lines say when they do not affect it.
 * Under the run: how it is still joining its warmup to the live stream, or
 * how it ended and what that left at the broker.
 */
@Component({
  selector: 'app-bot-health-groups',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [ExposureNoticesComponent, StartupJoinStatusComponent, TimestampDisplayComponent],
  templateUrl: './bot-health-groups.component.html',
  styleUrl: './bot-health-groups.component.scss',
})
export class BotHealthGroupsComponent {
  readonly health = input.required<BotHealthGroupsView>();
  /** The market for this bot's symbol right now, as the backend words it. */
  readonly marketPulse = input<MarketPulseView | null>(null);
  /** How the run is joining its warmup to the live stream (#2410), while it says. */
  readonly startupJoin = input<StartupJoinView | null>(null);
  /** How the run ended, and what it left at the broker, as the runner recorded it. */
  readonly dutyOutcome = input<BotHealthCard['duty_outcome']>(null);

  protected readonly groups = computed((): readonly { title: string; lines: readonly HealthLineView[] }[] => [
    { title: 'During this run', lines: this.health().run },
    { title: 'Account right now', lines: this.health().account },
  ]);
}
